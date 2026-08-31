// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MiniERC20, IERC20} from "./MiniERC20.sol";
import {IDlnSource, OrderCreation} from "./IDlnSource.sol";
import {ICopyRouter} from "./CopyVault.sol";

/// CopyVault v2 — multi-chain generalization of v1.
///
/// Identical to v1 (deposits, keeper-posted NAV, executor-only mirrorTrade,
/// in-kind redemption) EXCEPT the single hard-coded Solana "sleeve" is
/// generalized into a map of cross-chain DESTINATIONS keyed by deBridge
/// takeChainId. The executor can now fund a satellite on ANY chain on demand
/// (deBridge for EVM chains + Solana, CCTP handled off-chain for Arc), each
/// with its own owner-pinned receiver + take-token + cap. Just-in-time
/// funding: capital lives here as NAV and flows to a chain only when the
/// trader trades there.
///
/// The sleeve* functions are retained as thin wrappers over the Solana
/// destination so existing keeper code keeps working unchanged.
contract CopyVaultV2 is MiniERC20 {
    uint256 internal constant SOLANA = 7565164; // deBridge Solana chain id

    IERC20 public immutable asset;
    uint8 internal immutable assetDecimals;

    address public owner;
    address public executor;
    ICopyRouter public router;

    mapping(address => bool) public allowedTokens;
    address[] public heldTokens;
    mapping(address => uint256) internal heldIndex;

    uint256 public totalNavAsset;
    uint256 public navUpdatedAt;
    uint256 public navTtl = 15 minutes;

    uint256 public maxTradeBps = 500;
    uint256 public withdrawDelay = 1 hours;
    mapping(address => uint256) public lastDepositAt;

    // -------- cross-chain destinations (generalized sleeve) --------
    IDlnSource public dlnSource;

    struct Destination {
        bytes receiver;    // pinned recipient on the destination chain (EVM 20b / Solana 32b)
        bytes takeToken;   // token minted at the destination (its USDC)
        uint256 capBps;    // max share of NAV parked at this destination
        uint256 fundedAsset; // net asset units sent (cap tracker)
        bool set;
    }

    mapping(uint256 => Destination) internal dest; // keyed by deBridge takeChainId

    bool internal locked;

    event Deposit(address indexed from, uint256 assets, uint256 shares);
    event RedeemInKind(address indexed from, address indexed receiver, uint256 shares);
    event MirrorTrade(address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut);
    event NavPosted(uint256 totalNavAsset);
    event DestinationFunded(uint256 indexed chainId, uint256 amount, bytes32 orderId);
    event DestinationReturned(uint256 indexed chainId, uint256 amount);

    error NotOwner();
    error NotExecutor();
    error StaleNav();
    error TokenNotAllowed(address token);
    error TradeTooLarge();
    error WithdrawLocked();
    error Reentrancy();
    error DestNotConfigured();
    error DestCapExceeded();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyExecutor() {
        if (msg.sender != executor) revert NotExecutor();
        _;
    }

    modifier nonReentrant() {
        if (locked) revert Reentrancy();
        locked = true;
        _;
        locked = false;
    }

    constructor(IERC20 asset_, address executor_, ICopyRouter router_)
        MiniERC20("AvgJoes Copy Vault", "avgJOE", 18)
    {
        asset = asset_;
        assetDecimals = asset_.decimals();
        owner = msg.sender;
        executor = executor_;
        router = router_;
    }

    // ---------------------------------------------------------------- admin

    function setExecutor(address e) external onlyOwner {
        executor = e;
    }

    function setRouter(ICopyRouter r) external onlyOwner {
        router = r;
    }

    function setAllowedToken(address token, bool allowed) external onlyOwner {
        allowedTokens[token] = allowed;
    }

    function setParams(uint256 maxTradeBps_, uint256 withdrawDelay_, uint256 navTtl_) external onlyOwner {
        require(maxTradeBps_ <= 10_000, "bps");
        maxTradeBps = maxTradeBps_;
        withdrawDelay = withdrawDelay_;
        navTtl = navTtl_;
    }

    function setDlnSource(IDlnSource dln) external onlyOwner {
        dlnSource = dln;
    }

    /// Configure (or update) a cross-chain destination. receiver is the pinned
    /// recipient: 20 bytes for an EVM chain, 32 for Solana.
    function setDestination(uint256 chainId, bytes calldata receiver, bytes calldata takeToken, uint256 capBps)
        external
        onlyOwner
    {
        require(receiver.length == 20 || receiver.length == 32, "receiver");
        require(takeToken.length == 20 || takeToken.length == 32, "takeToken");
        require(capBps <= 10_000, "bps");
        Destination storage dd = dest[chainId];
        dd.receiver = receiver;
        dd.takeToken = takeToken;
        dd.capBps = capBps;
        dd.set = true;
    }

    // ---------------------------------------------------------------- keeper

    function postNav(uint256 totalNavAsset_) external onlyExecutor {
        totalNavAsset = totalNavAsset_;
        navUpdatedAt = block.timestamp;
        emit NavPosted(totalNavAsset_);
    }

    function mirrorTrade(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut)
        external
        onlyExecutor
        nonReentrant
        returns (uint256 amountOut)
    {
        require(tokenIn != tokenOut, "same token");
        if (tokenIn == address(asset)) {
            if (!allowedTokens[tokenOut]) revert TokenNotAllowed(tokenOut);
            if (_navStale()) revert StaleNav();
            if (amountIn > totalNavAsset * maxTradeBps / 10_000) revert TradeTooLarge();
        } else if (tokenOut == address(asset)) {
            if (heldIndex[tokenIn] == 0) revert TokenNotAllowed(tokenIn);
        } else {
            revert TokenNotAllowed(tokenOut);
        }

        IERC20(tokenIn).approve(address(router), amountIn);
        amountOut = router.swap(tokenIn, tokenOut, amountIn, minOut, address(this));
        require(amountOut >= minOut, "slippage");

        if (tokenIn == address(asset)) _noteHeld(tokenOut);
        else if (IERC20(tokenIn).balanceOf(address(this)) == 0) _dropHeld(tokenIn);

        emit MirrorTrade(tokenIn, tokenOut, amountIn, amountOut);
    }

    /// Bridge `amount` of the asset to a configured destination via a DLN order
    /// the vault creates itself. Receiver/take-token/chain are pinned by the
    /// owner-set destination config; the executor only chooses timing + size,
    /// bounded by the destination's cap.
    function fundDestination(uint256 chainId, uint256 amount, uint256 takeAmountMin)
        external
        payable
        onlyExecutor
        nonReentrant
        returns (bytes32 orderId)
    {
        return _fundDestination(chainId, amount, takeAmountMin);
    }

    function _fundDestination(uint256 chainId, uint256 amount, uint256 takeAmountMin)
        internal
        returns (bytes32 orderId)
    {
        Destination storage dd = dest[chainId];
        if (!dd.set) revert DestNotConfigured();
        if (_navStale()) revert StaleNav();
        if (dd.fundedAsset + amount > totalNavAsset * dd.capBps / 10_000) revert DestCapExceeded();
        dd.fundedAsset += amount;

        asset.approve(address(dlnSource), amount);
        orderId = dlnSource.createOrder{value: msg.value}(
            OrderCreation({
                giveTokenAddress: address(asset),
                giveAmount: amount,
                takeTokenAddress: dd.takeToken,
                takeAmount: takeAmountMin,
                takeChainId: chainId,
                receiverDst: dd.receiver,
                givePatchAuthoritySrc: address(this),
                orderAuthorityAddressDst: dd.receiver,
                allowedTakerDst: "",
                externalCall: "",
                allowedCancelBeneficiarySrc: ""
            }),
            "",
            0,
            ""
        );
        emit DestinationFunded(chainId, amount, orderId);
    }

    /// Keeper attests a destination returned `amount` of the asset to the vault.
    function noteDestinationReturn(uint256 chainId, uint256 amount) external onlyExecutor {
        Destination storage dd = dest[chainId];
        dd.fundedAsset = amount >= dd.fundedAsset ? 0 : dd.fundedAsset - amount;
        emit DestinationReturned(chainId, amount);
    }

    // -------- sleeve* wrappers (backward compat: Solana destination) --------

    function setSleeve(IDlnSource dln, bytes calldata receiver, bytes calldata takeToken, uint256 capBps)
        external
        onlyOwner
    {
        dlnSource = dln;
        Destination storage dd = dest[SOLANA];
        dd.receiver = receiver;
        dd.takeToken = takeToken;
        dd.capBps = capBps;
        dd.set = true;
    }

    function fundSleeve(uint256 amount, uint256 takeAmountMin)
        external
        payable
        onlyExecutor
        nonReentrant
        returns (bytes32)
    {
        return _fundDestination(SOLANA, amount, takeAmountMin);
    }

    function noteSleeveReturn(uint256 amount) external onlyExecutor {
        Destination storage dd = dest[SOLANA];
        dd.fundedAsset = amount >= dd.fundedAsset ? 0 : dd.fundedAsset - amount;
        emit DestinationReturned(SOLANA, amount);
    }

    // ---------------------------------------------------------------- users

    function deposit(uint256 assets) external nonReentrant returns (uint256 shares) {
        require(assets > 0, "zero");
        if (totalSupply == 0) {
            shares = assets * 1e18 / 10 ** assetDecimals;
            totalNavAsset = 0;
        } else {
            if (_navStale()) revert StaleNav();
            require(totalNavAsset > 0, "nav zero");
            shares = assets * totalSupply / totalNavAsset;
        }
        totalNavAsset += assets;
        lastDepositAt[msg.sender] = block.timestamp;
        require(asset.transferFrom(msg.sender, address(this), assets), "transfer");
        _mint(msg.sender, shares);
        emit Deposit(msg.sender, assets, shares);
    }

    function redeemInKind(uint256 shares, address receiver) external nonReentrant {
        require(shares > 0 && shares <= balanceOf[msg.sender], "shares");
        if (block.timestamp < lastDepositAt[msg.sender] + withdrawDelay) revert WithdrawLocked();

        uint256 supplyBefore = totalSupply;
        totalNavAsset -= totalNavAsset * shares / supplyBefore;
        _burn(msg.sender, shares);

        uint256 assetSlice = asset.balanceOf(address(this)) * shares / supplyBefore;
        if (assetSlice > 0) require(asset.transfer(receiver, assetSlice), "transfer");

        for (uint256 i = heldTokens.length; i > 0; i--) {
            IERC20 token = IERC20(heldTokens[i - 1]);
            uint256 slice = token.balanceOf(address(this)) * shares / supplyBefore;
            if (slice > 0) require(token.transfer(receiver, slice), "transfer");
            if (token.balanceOf(address(this)) == 0) _dropHeld(address(token));
        }
        emit RedeemInKind(msg.sender, receiver, shares);
    }

    // ---------------------------------------------------------------- views

    function heldTokensLength() external view returns (uint256) {
        return heldTokens.length;
    }

    function destination(uint256 chainId)
        external
        view
        returns (bytes memory receiver, bytes memory takeToken, uint256 capBps, uint256 fundedAsset, bool set)
    {
        Destination storage dd = dest[chainId];
        return (dd.receiver, dd.takeToken, dd.capBps, dd.fundedAsset, dd.set);
    }

    // legacy view shims used by existing keeper code (Solana destination)
    function sleeveReceiver() external view returns (bytes memory) {
        return dest[SOLANA].receiver;
    }

    function sleeveTakeToken() external view returns (bytes memory) {
        return dest[SOLANA].takeToken;
    }

    function sleeveCapBps() external view returns (uint256) {
        return dest[SOLANA].capBps;
    }

    function sleeveFundedAsset() external view returns (uint256) {
        return dest[SOLANA].fundedAsset;
    }

    function _navStale() internal view returns (bool) {
        return block.timestamp > navUpdatedAt + navTtl;
    }

    function _noteHeld(address token) internal {
        if (heldIndex[token] == 0) {
            heldTokens.push(token);
            heldIndex[token] = heldTokens.length;
        }
    }

    function _dropHeld(address token) internal {
        uint256 idx = heldIndex[token];
        if (idx == 0) return;
        uint256 lastIdx = heldTokens.length;
        if (idx != lastIdx) {
            address moved = heldTokens[lastIdx - 1];
            heldTokens[idx - 1] = moved;
            heldIndex[moved] = idx;
        }
        heldTokens.pop();
        heldIndex[token] = 0;
    }
}
