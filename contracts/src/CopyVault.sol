// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MiniERC20, IERC20} from "./MiniERC20.sol";
import {IDlnSource, OrderCreation} from "./IDlnSource.sol";

/// Minimal swap interface the vault trades through. Concrete adapters
/// (Uniswap V3, mock DEX on testnet) implement it.
interface ICopyRouter {
    function swap(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, address to)
        external
        returns (uint256 amountOut);
}

/// Single-trader copytrading vault, prototype.
///
/// - Deposits in one asset (e.g. USDC); shares priced off a keeper-posted NAV
///   with a freshness requirement.
/// - The keeper (executor) mirrors the tracked trader via mirrorTrade(),
///   bounded by on-chain guardrails (token allowlist, max % of NAV per buy).
/// - Redemption is IN KIND: burning shares pays out a pro-rata slice of the
///   asset and every held token, so exits never depend on NAV pricing.
contract CopyVault is MiniERC20 {
    IERC20 public immutable asset;
    uint8 internal immutable assetDecimals;

    address public owner;
    address public executor;
    ICopyRouter public router;

    mapping(address => bool) public allowedTokens;
    address[] public heldTokens; // tokens the vault holds besides `asset`
    mapping(address => uint256) internal heldIndex; // index+1, 0 = not held

    uint256 public totalNavAsset; // keeper-posted NAV of the whole vault, in asset units
    uint256 public navUpdatedAt;
    uint256 public navTtl = 15 minutes;

    uint256 public maxTradeBps = 500; // max buy size vs NAV
    uint256 public withdrawDelay = 1 hours;
    mapping(address => uint256) public lastDepositAt;

    // -------- Solana sleeve (cross-chain execution) --------
    // The vault funds a fixed Solana address ("the sleeve") through deBridge
    // DLN orders it creates itself, so the executor can choose WHEN and HOW
    // MUCH to fund but can never redirect funds: the receiver is pinned here.
    IDlnSource public dlnSource;
    bytes public sleeveReceiver; // 32-byte Solana pubkey of the sleeve wallet
    bytes public sleeveTakeToken; // 32-byte mint the sleeve receives (USDC)
    uint256 public sleeveChainId = 7565164; // deBridge's Solana chain id
    uint256 public sleeveCapBps = 3000; // max share of NAV parked in the sleeve
    uint256 public sleeveFundedAsset; // net asset units sent to the sleeve

    bool internal locked;

    event Deposit(address indexed from, uint256 assets, uint256 shares);
    event RedeemInKind(address indexed from, address indexed receiver, uint256 shares);
    event MirrorTrade(address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut);
    event NavPosted(uint256 totalNavAsset);
    event SleeveFunded(uint256 amount, bytes32 orderId);
    event SleeveReturned(uint256 amount);

    error NotOwner();
    error NotExecutor();
    error StaleNav();
    error TokenNotAllowed(address token);
    error TradeTooLarge();
    error WithdrawLocked();
    error Reentrancy();
    error SleeveNotConfigured();
    error SleeveCapExceeded();

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

    function setSleeve(IDlnSource dln, bytes calldata receiver, bytes calldata takeToken, uint256 capBps)
        external
        onlyOwner
    {
        require(receiver.length == 32 && takeToken.length == 32, "pubkey");
        require(capBps <= 10_000, "bps");
        dlnSource = dln;
        sleeveReceiver = receiver;
        sleeveTakeToken = takeToken;
        sleeveCapBps = capBps;
    }

    // ---------------------------------------------------------------- keeper

    /// Keeper posts the vault's total value in asset units. Gates deposits;
    /// never gates in-kind redemption.
    function postNav(uint256 totalNavAsset_) external onlyExecutor {
        totalNavAsset = totalNavAsset_;
        navUpdatedAt = block.timestamp;
        emit NavPosted(totalNavAsset_);
    }

    /// Mirror one trade of the tracked trader. One side must be `asset`.
    function mirrorTrade(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut)
        external
        onlyExecutor
        nonReentrant
        returns (uint256 amountOut)
    {
        require(tokenIn != tokenOut, "same token");
        if (tokenIn == address(asset)) {
            // buy: allowlisted target, capped fraction of NAV
            if (!allowedTokens[tokenOut]) revert TokenNotAllowed(tokenOut);
            if (_navStale()) revert StaleNav();
            if (amountIn > totalNavAsset * maxTradeBps / 10_000) revert TradeTooLarge();
        } else if (tokenOut == address(asset)) {
            // sell: only positions the vault actually holds
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

    /// Send `amount` of the asset to the fixed Solana sleeve via a DLN order.
    /// The executor picks timing and size; the receiver, destination chain and
    /// received token are pinned by the owner-set sleeve config.
    function fundSleeve(uint256 amount, uint256 takeAmountMin)
        external
        payable
        onlyExecutor
        nonReentrant
        returns (bytes32 orderId)
    {
        if (sleeveReceiver.length == 0) revert SleeveNotConfigured();
        if (_navStale()) revert StaleNav();
        if (sleeveFundedAsset + amount > totalNavAsset * sleeveCapBps / 10_000) {
            revert SleeveCapExceeded();
        }
        sleeveFundedAsset += amount;

        asset.approve(address(dlnSource), amount);
        orderId = dlnSource.createOrder{value: msg.value}(
            OrderCreation({
                giveTokenAddress: address(asset),
                giveAmount: amount,
                takeTokenAddress: sleeveTakeToken,
                takeAmount: takeAmountMin,
                takeChainId: sleeveChainId,
                receiverDst: sleeveReceiver,
                givePatchAuthoritySrc: address(this),
                orderAuthorityAddressDst: sleeveReceiver,
                allowedTakerDst: "",
                externalCall: "",
                allowedCancelBeneficiarySrc: ""
            }),
            "",
            0,
            ""
        );
        emit SleeveFunded(amount, orderId);
    }

    /// Keeper attests that the sleeve returned `amount` of the asset to the
    /// vault (bridged back). Trusted accounting, bounded by the sleeve cap.
    function noteSleeveReturn(uint256 amount) external onlyExecutor {
        sleeveFundedAsset = amount >= sleeveFundedAsset ? 0 : sleeveFundedAsset - amount;
        emit SleeveReturned(amount);
    }

    // ---------------------------------------------------------------- users

    function deposit(uint256 assets) external nonReentrant returns (uint256 shares) {
        require(assets > 0, "zero");
        if (totalSupply == 0) {
            // bootstrap: 1 asset unit = 1 share (share has 18 decimals)
            shares = assets * 1e18 / 10 ** assetDecimals;
            totalNavAsset = 0; // any stale value is meaningless with no shares
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

    /// Burn shares for a pro-rata slice of the asset and every held token.
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
