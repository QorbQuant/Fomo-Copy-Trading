// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MiniERC20, IERC20} from "./MiniERC20.sol";
import {IDlnSource, OrderCreation} from "./IDlnSource.sol";
import {ICopyRouter} from "./CopyVault.sol";

interface IWETH {
    function withdraw(uint256) external;
    function balanceOf(address) external view returns (uint256);
}

/// CopyVault v3 — public-deposit hardening of v2.
///
/// Keeps every v2 mechanic (USDG deposits, keeper-posted NAV, executor-only
/// mirrorTrade, cross-chain destinations + Solana sleeve wrappers, in-kind
/// redemption) and adds what a multi-depositor pool needs:
///
///  - Role separation: owner (config) / executor (keeper) / guardian (pause).
///    Ownership transfer is 2-step. A leaked keeper can no longer re-point the
///    router or reconfigure destinations — those are owner-only.
///  - Pause: guardian OR owner can halt deposits, trades and funding on an
///    alert; redemptions are NEVER pausable, so funds can't be trapped.
///  - On-chain NAV bounds: postNav rejects a mark that jumps more than
///    maxNavDeviationBps from the last, so a fat-fingered / manipulated NAV
///    can't be minted or redeemed against. Owner has an override for genuine
///    large moves. Off by default (set once real volatility is observed).
///  - Deposit slippage floor: deposit(assets, minSharesOut) reverts if a NAV
///    post front-runs the depositor into fewer shares than they accepted.
///  - Fees: an optional deposit fee and an optional performance fee with a
///    global high-water mark, crystallized as shares minted to the treasury so
///    everything stays in-kind. Both default to 0 (dormant until configured).
///  - NAV-funded gas: sweepGas swaps a bounded slice of USDG to ETH for the
///    keeper, so the system sustains its own gas out of NAV instead of the
///    owner topping it up by hand. Bounded by bps-of-NAV + a cooldown, and it
///    can only pay the pinned executor.
///  - Bounded withdrawDelay (<= 7 days) so redemptions can't be locked forever.
///  - Away-aware in-kind redemption: fixes v2's cross-chain haircut. v2 burned
///    the full share (home + away) but paid only home tokens, silently
///    forfeiting a redeemer's Solana/satellite slice to everyone else. v3 pays
///    the full pro-rata home slice, burns only the home-backed fraction of the
///    shares, and leaves the away fraction as shares the redeemer keeps and
///    redeems once the keeper repatriates that capital. PPS is preserved for
///    every party.
contract CopyVaultV3 is MiniERC20 {
    uint256 internal constant SOLANA = 7565164; // deBridge Solana chain id
    uint256 public constant MAX_WITHDRAW_DELAY = 7 days;
    uint256 internal constant WAD = 1e18;
    uint256 internal constant YEAR = 365 days;

    IERC20 public immutable asset;
    IWETH public immutable weth;
    uint8 internal immutable assetDecimals;

    address public owner;
    address public pendingOwner;
    address public executor;
    address public guardian;
    address public treasury;
    ICopyRouter public router;

    bool public paused;

    mapping(address => bool) public allowedTokens;
    address[] public heldTokens;
    mapping(address => uint256) internal heldIndex;

    uint256 public totalNavAsset;
    uint256 public awayNav; // portion of NAV living on other chains (keeper-attested)
    uint256 public navUpdatedAt;
    uint256 public navTtl = 15 minutes;
    uint256 public maxNavDeviationBps; // 0 = bound disabled

    uint256 public maxTradeBps = 500;
    uint256 public withdrawDelay = 1 hours;
    mapping(address => uint256) public lastDepositAt;

    /// Minimum value a cross-chain funding order must promise to deliver, as bps
    /// of the amount given away. Blocks a leaked keeper from posting takeAmountMin
    /// ~0 and letting a taker fill the DLN order for nothing. Assumes give/take
    /// tokens share decimals (true for the USDC/USDG family used here).
    uint256 public minReturnBps = 9000;

    /// Temporary per-address deposit cap (asset units; 0 = no cap). A training
    /// wheel for the un-audited beta: bounds how much any single address can put
    /// at risk. depositedAssets tracks live principal per address — it grows on
    /// deposit and shrinks pro-rata on redeem, so an address that fully exits can
    /// deposit again. Owner sets it to 0 to lift the cap once audited. Not sybil-
    /// proof (deposits can be split across addresses); it caps per-address size,
    /// not total exposure.
    uint256 public maxDepositPerAddress;
    mapping(address => uint256) public depositedAssets;

    // fees
    uint256 public depositFeeBps;  // <= 200 (2%): one-time entry fee
    uint256 public mgmtFeeBps;     // <= 500 (5%/yr): continuous AUM fee, streamed to treasury
    uint256 public perfFeeBps;     // <= 2000 (20%): on gains above the high-water mark
    uint256 public hwm;            // high-water price-per-share (WAD-scaled)
    uint256 public lastAccrualTs;  // last time the management fee was crystallized

    // NAV-funded gas
    uint256 public maxGasSweepBps;   // <= 100 (1% of NAV per sweep); 0 = disabled
    uint256 public gasSweepCooldown = 1 hours;
    uint256 public lastGasSweep;

    // cross-chain destinations (generalized sleeve)
    IDlnSource public dlnSource;

    struct Destination {
        bytes receiver;
        bytes takeToken;
        uint256 capBps;
        uint256 fundedAsset;
        bool set;
    }

    mapping(uint256 => Destination) internal dest;

    bool internal locked;

    event Deposit(address indexed from, uint256 assets, uint256 shares);
    event RedeemInKind(address indexed from, address indexed receiver, uint256 shares, uint256 sharesBurned);
    event MirrorTrade(address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut);
    event NavPosted(uint256 totalNavAsset, uint256 awayNav);
    event DestinationFunded(uint256 indexed chainId, uint256 amount, bytes32 orderId);
    event DestinationReturned(uint256 indexed chainId, uint256 amount);
    event Paused(address indexed by);
    event Unpaused(address indexed by);
    event DepositFeeTaken(uint256 fee);
    event PerfFeeCrystallized(uint256 feeShares, uint256 feeAssets);
    event MgmtFeeAccrued(uint256 feeShares, uint256 feeAssets);
    event GasSwept(uint256 usdgIn, uint256 ethOut);
    event MaxDepositPerAddressSet(uint256 cap);
    event OwnershipTransferStarted(address indexed newOwner);
    event OwnershipTransferred(address indexed newOwner);

    error NotOwner();
    error NotExecutor();
    error NotGuardian();
    error IsPaused();
    error StaleNav();
    error NavBounds();
    error TokenNotAllowed(address token);
    error TradeTooLarge();
    error WithdrawLocked();
    error Reentrancy();
    error DestNotConfigured();
    error DestCapExceeded();
    error SlippageTooHigh();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyExecutor() {
        if (msg.sender != executor) revert NotExecutor();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert IsPaused();
        _;
    }

    modifier nonReentrant() {
        if (locked) revert Reentrancy();
        locked = true;
        _;
        locked = false;
    }

    constructor(
        IERC20 asset_,
        IWETH weth_,
        address executor_,
        address guardian_,
        address treasury_,
        ICopyRouter router_
    ) MiniERC20("AvgJoes Copy Vault", "avgJOE", 18) {
        asset = asset_;
        weth = weth_;
        assetDecimals = asset_.decimals();
        owner = msg.sender;
        executor = executor_;
        guardian = guardian_;
        treasury = treasury_;
        router = router_;
        lastAccrualTs = block.timestamp;
    }

    // ---------------------------------------------------------------- ownership

    function transferOwnership(address newOwner) external onlyOwner {
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(newOwner);
    }

    function acceptOwnership() external {
        require(msg.sender == pendingOwner, "not pending");
        owner = pendingOwner;
        pendingOwner = address(0);
        emit OwnershipTransferred(owner);
    }

    // ---------------------------------------------------------------- admin

    function setExecutor(address e) external onlyOwner {
        executor = e;
    }

    function setGuardian(address g) external onlyOwner {
        guardian = g;
    }

    function setTreasury(address t) external onlyOwner {
        treasury = t;
    }

    function setRouter(ICopyRouter r) external onlyOwner {
        router = r;
    }

    function setAllowedToken(address token, bool allowed) external onlyOwner {
        allowedTokens[token] = allowed;
    }

    function setMinReturnBps(uint256 bps) external onlyOwner {
        require(bps <= 10_000, "bps");
        minReturnBps = bps;
    }

    /// Temporary beta deposit cap per address (asset units). 0 lifts the cap.
    function setMaxDepositPerAddress(uint256 cap) external onlyOwner {
        maxDepositPerAddress = cap;
        emit MaxDepositPerAddressSet(cap);
    }

    /// Eject a token from the redemption payout set even while the vault still
    /// holds a nonzero balance of it. Escape hatch for a held token that has
    /// turned hostile (pausable/blacklisting/reverting) so it can no longer
    /// block every redeemer; the keeper simply stops marking it in NAV.
    function quarantineHeldToken(address token) external onlyOwner {
        _dropHeld(token);
    }

    function setParams(uint256 maxTradeBps_, uint256 withdrawDelay_, uint256 navTtl_) external onlyOwner {
        require(maxTradeBps_ <= 10_000, "bps");
        require(withdrawDelay_ <= MAX_WITHDRAW_DELAY, "delay");
        maxTradeBps = maxTradeBps_;
        withdrawDelay = withdrawDelay_;
        navTtl = navTtl_;
    }

    function setFees(uint256 depositFeeBps_, uint256 mgmtFeeBps_, uint256 perfFeeBps_) external onlyOwner {
        require(depositFeeBps_ <= 200, "deposit fee");   // 2% entry cap
        require(mgmtFeeBps_ <= 500, "mgmt fee");         // 5%/yr AUM cap
        require(perfFeeBps_ <= 2000, "perf fee");        // 20% performance cap
        // accrue at the OLD rates before changing, so a rate change can't
        // retroactively re-price fees already earned.
        _accrueFees();
        depositFeeBps = depositFeeBps_;
        mgmtFeeBps = mgmtFeeBps_;
        perfFeeBps = perfFeeBps_;
    }

    function setNavBounds(uint256 maxNavDeviationBps_) external onlyOwner {
        require(maxNavDeviationBps_ <= 10_000, "bps");
        maxNavDeviationBps = maxNavDeviationBps_;
    }

    function setGasSweep(uint256 maxGasSweepBps_, uint256 cooldown_) external onlyOwner {
        require(maxGasSweepBps_ <= 100, "bps");        // hard ceiling 1% of NAV per sweep
        require(cooldown_ >= 15 minutes, "cooldown");  // floor so the sweep can't be looped in one block
        maxGasSweepBps = maxGasSweepBps_;
        gasSweepCooldown = cooldown_;
    }

    function setDlnSource(IDlnSource dln) external onlyOwner {
        dlnSource = dln;
    }

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

    // ---------------------------------------------------------------- guardian

    function pause() external {
        if (msg.sender != guardian && msg.sender != owner) revert NotGuardian();
        paused = true;
        emit Paused(msg.sender);
    }

    /// Only the owner can resume — a higher bar than the freeze.
    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused(msg.sender);
    }

    // ---------------------------------------------------------------- keeper

    function postNav(uint256 total, uint256 away) external onlyExecutor {
        require(away <= total, "away>total");
        _checkNavBounds(total);
        totalNavAsset = total;
        awayNav = away;
        navUpdatedAt = block.timestamp;
        emit NavPosted(total, away);
    }

    /// Owner escape hatch for a genuine large move the bound would reject, or to
    /// re-seed NAV after a bound-induced stall.
    function postNavOverride(uint256 total, uint256 away) external onlyOwner {
        require(away <= total, "away>total");
        totalNavAsset = total;
        awayNav = away;
        navUpdatedAt = block.timestamp;
        emit NavPosted(total, away);
    }

    function _checkNavBounds(uint256 total) internal view {
        uint256 prev = totalNavAsset;
        if (maxNavDeviationBps == 0 || prev == 0) return;
        uint256 band = prev * maxNavDeviationBps / 10_000;
        if (total > prev + band || total < prev - band) revert NavBounds();
    }

    function mirrorTrade(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut)
        external
        onlyExecutor
        whenNotPaused
        nonReentrant
        returns (uint256 amountOut)
    {
        require(tokenIn != tokenOut, "same token");
        require(minOut > 0, "minOut"); // no blind swaps: force the keeper to state a floor
        if (tokenIn == address(asset)) {
            if (!allowedTokens[tokenOut]) revert TokenNotAllowed(tokenOut);
            if (_navStale()) revert StaleNav();
            // Cap against the real USDG balance, not keeper-posted NAV, so the
            // executor can't lift its own trade ceiling by posting a large NAV.
            if (amountIn > asset.balanceOf(address(this)) * maxTradeBps / 10_000) revert TradeTooLarge();
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

    function fundDestination(uint256 chainId, uint256 amount, uint256 takeAmountMin)
        external
        payable
        onlyExecutor
        whenNotPaused
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
        // The order must promise to deliver at least minReturnBps of what it gives
        // away, so a leaked keeper can't create a ~free giveaway for a taker.
        require(takeAmountMin >= amount * minReturnBps / 10_000, "return floor");
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

    function noteDestinationReturn(uint256 chainId, uint256 amount) external onlyExecutor {
        Destination storage dd = dest[chainId];
        dd.fundedAsset = amount >= dd.fundedAsset ? 0 : dd.fundedAsset - amount;
        // Repatriated capital is home again: shrink awayNav in step so hNav tracks
        // the returned balance immediately, closing the window where a redeemer
        // could take the (now higher) home balance while too few shares burn.
        awayNav = amount >= awayNav ? 0 : awayNav - amount;
        emit DestinationReturned(chainId, amount);
    }

    /// Swap a bounded slice of vault USDG to ETH for the keeper's gas. Sustains
    /// the system's running cost out of NAV; can only pay the pinned executor,
    /// bounded by bps-of-NAV and a cooldown.
    function sweepGas(uint256 usdgAmount, uint256 minEthOut)
        external
        onlyExecutor
        whenNotPaused
        nonReentrant
    {
        require(maxGasSweepBps > 0, "disabled");
        if (_navStale()) revert StaleNav();
        require(usdgAmount <= totalNavAsset * maxGasSweepBps / 10_000, "too much");
        require(block.timestamp >= lastGasSweep + gasSweepCooldown, "cooldown");
        lastGasSweep = block.timestamp;

        asset.approve(address(router), usdgAmount);
        uint256 wethOut = router.swap(address(asset), address(weth), usdgAmount, minEthOut, address(this));
        if (wethOut < minEthOut) revert SlippageTooHigh();
        weth.withdraw(wethOut);

        // gas is a running expense borne pro-rata by all holders
        totalNavAsset -= usdgAmount;

        (bool ok,) = executor.call{value: wethOut}("");
        require(ok, "eth send");
        emit GasSwept(usdgAmount, wethOut);
    }

    receive() external payable {}

    /// Recover stray ETH or a token that was sent to the vault outside the trade
    /// path. Restricted so it can never touch managed funds: not the asset, and
    /// not anything currently in the redemption payout set.
    function rescue(address token, address to) external onlyOwner {
        require(to != address(0), "to");
        if (token == address(0)) {
            (bool ok,) = to.call{value: address(this).balance}("");
            require(ok, "eth");
        } else {
            require(token != address(asset) && heldIndex[token] == 0, "managed");
            require(IERC20(token).transfer(to, IERC20(token).balanceOf(address(this))), "rescue");
        }
    }

    /// Make the redemption withdraw-delay follow the shares: any address that
    /// receives shares (mint or transfer) starts its own delay, so the lock can't
    /// be dodged by minting at a stale NAV and moving the shares to a fresh key.
    function _afterTokenTransfer(address, address to, uint256 amount) internal override {
        if (to != address(0) && amount > 0) lastDepositAt[to] = block.timestamp;
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
        whenNotPaused
        nonReentrant
        returns (bytes32)
    {
        return _fundDestination(SOLANA, amount, takeAmountMin);
    }

    function noteSleeveReturn(uint256 amount) external onlyExecutor {
        Destination storage dd = dest[SOLANA];
        dd.fundedAsset = amount >= dd.fundedAsset ? 0 : dd.fundedAsset - amount;
        awayNav = amount >= awayNav ? 0 : awayNav - amount; // keep hNav in step (see noteDestinationReturn)
        emit DestinationReturned(SOLANA, amount);
    }

    // ---------------------------------------------------------------- users

    function deposit(uint256 assets, uint256 minSharesOut)
        external
        whenNotPaused
        nonReentrant
        returns (uint256 shares)
    {
        require(assets > 0, "zero");
        if (maxDepositPerAddress > 0) {
            uint256 principal = depositedAssets[msg.sender] + assets;
            require(principal <= maxDepositPerAddress, "deposit cap");
            depositedAssets[msg.sender] = principal;
        }
        require(asset.transferFrom(msg.sender, address(this), assets), "transfer");

        uint256 fee = assets * depositFeeBps / 10_000;
        uint256 net = assets - fee;
        if (fee > 0 && treasury != address(0)) {
            require(asset.transfer(treasury, fee), "fee");
            emit DepositFeeTaken(fee);
        }

        if (totalSupply == 0) {
            shares = net * WAD / 10 ** assetDecimals;
            totalNavAsset = net;
            _mint(msg.sender, shares);
            hwm = _pps();                    // seed the high-water mark at the opening price
            lastAccrualTs = block.timestamp; // start the AUM clock at first capital
        } else {
            if (_navStale()) revert StaleNav();
            require(totalNavAsset > 0, "nav zero");
            _accrueFees(); // charge accrued AUM + perf fees before new money enters
            // Floor the pricing denominator at the real pre-deposit USDG on hand.
            // A manipulated low NAV can then never mint more shares than the vault
            // is actually backed by, since the denominator can't drop below cash.
            uint256 preUsdg = asset.balanceOf(address(this)) - net;
            uint256 denom = totalNavAsset > preUsdg ? totalNavAsset : preUsdg;
            shares = net * totalSupply / denom;
            totalNavAsset += net;
            _mint(msg.sender, shares);
        }

        require(shares >= minSharesOut, "minSharesOut");
        lastDepositAt[msg.sender] = block.timestamp;
        emit Deposit(msg.sender, assets, shares);
    }

    /// In-kind redemption, away-aware. Pays the redeemer's full pro-rata slice
    /// of every home token, burns only the home-backed fraction of the shares,
    /// and leaves the away fraction as shares they keep — redeemable once the
    /// keeper bridges that capital home. Never pausable.
    function redeemInKind(uint256 shares, address receiver) external nonReentrant {
        require(shares > 0 && shares <= balanceOf[msg.sender], "shares");
        if (block.timestamp < lastDepositAt[msg.sender] + withdrawDelay) revert WithdrawLocked();

        _accrueFees();
        uint256 supplyBefore = totalSupply;
        uint256 nav = totalNavAsset;
        uint256 hNav = nav > awayNav ? nav - awayNav : 0;

        uint256 sharesToBurn;
        if (hNav > 0) {
            // Away-aware path: burn only the home-backed fraction and mark out
            // exactly the home value paid; the away fraction stays as shares the
            // redeemer keeps and redeems once that capital is repatriated.
            sharesToBurn = shares * hNav / nav;
            require(sharesToBurn > 0, "dust");
            totalNavAsset = nav - hNav * shares / supplyBefore;
        } else {
            // Degenerate: the posted split says nothing is home. Never brick the
            // exit — fall back to a full-share burn against whatever is actually
            // on hand, so a bad NAV post can't trap funds.
            sharesToBurn = shares;
            totalNavAsset = nav > 0 ? nav - nav * shares / supplyBefore : 0;
        }

        // Release tracked deposit principal in proportion to the shares burned, so
        // an address that exits frees up room under the per-address cap.
        if (maxDepositPerAddress > 0) {
            uint256 tracked = depositedAssets[msg.sender];
            if (tracked > 0) {
                uint256 reduce = tracked * sharesToBurn / balanceOf[msg.sender]; // pre-burn balance
                depositedAssets[msg.sender] = tracked > reduce ? tracked - reduce : 0;
            }
        }
        _burn(msg.sender, sharesToBurn);

        uint256 assetSlice = asset.balanceOf(address(this)) * shares / supplyBefore;
        if (assetSlice > 0) require(asset.transfer(receiver, assetSlice), "transfer");

        // Pay each held token best-effort. A single hostile token (paused,
        // blacklisting, reverting, no-return) is skipped rather than reverting the
        // whole redemption; its slice stays in the vault, claimable after the owner
        // quarantines it or it recovers. Redemption can never be bricked by a token.
        for (uint256 i = heldTokens.length; i > 0; i--) {
            address token = heldTokens[i - 1];
            uint256 bal;
            try IERC20(token).balanceOf(address(this)) returns (uint256 b) {
                bal = b;
            } catch {
                continue;
            }
            uint256 slice = bal * shares / supplyBefore;
            if (slice > 0) {
                try IERC20(token).transfer(receiver, slice) returns (bool ok) {
                    if (!ok) continue;
                } catch {
                    continue;
                }
            }
            if (bal - slice == 0) _dropHeld(token);
        }
        emit RedeemInKind(msg.sender, receiver, shares, sharesToBurn);
    }

    // ---------------------------------------------------------------- fees

    function _pps() internal view returns (uint256) {
        if (totalSupply == 0) return 0;
        return totalNavAsset * WAD / totalSupply;
    }

    /// Permissionless: crystallize the time-based AUM fee (and any performance
    /// fee) into treasury shares. Callable by anyone — the keeper runs it on a
    /// cadence so the management fee streams steadily even during quiet stretches
    /// with no deposits or redemptions. It can only mint fee shares to the
    /// treasury, so there is no caller-side advantage to abuse.
    function accrue() external {
        _accrueFees();
    }

    /// Crystallize both fees as shares minted to the treasury (keeps redemption
    /// fully in-kind — no loose USDG is required to charge a fee):
    ///  1. Management (AUM) fee: mgmtFeeBps of NAV per year, pro-rated over the
    ///     elapsed time since the last accrual.
    ///  2. Performance fee: perfFeeBps of gains above the high-water mark. HWM is
    ///     set to the gross price at crystallization so a gain band is taxed once.
    /// Both mint via dilution: feeShares s.t. the treasury's new shares are worth
    /// feeAssets at the current mark. Management accrues first, so the perf fee is
    /// taken on performance net of the management fee.
    function _accrueFees() internal {
        if (totalSupply == 0 || totalNavAsset == 0) {
            lastAccrualTs = block.timestamp;
            return;
        }

        // 1. management / AUM fee — streamed by elapsed time. Only advance the
        //    accrual clock when a fee actually mints: on a small vault (or under
        //    frequent accrue() calls) a short dt truncates feeAssets to 0, and
        //    advancing the clock then would silently drop that elapsed time. By
        //    leaving lastAccrualTs put on a zero-round, dt keeps growing until it
        //    rounds to a chargeable amount, so the fee can't be starved.
        if (mgmtFeeBps == 0 || treasury == address(0)) {
            lastAccrualTs = block.timestamp; // fee off: keep the clock current (no back-bill when enabled)
        } else {
            uint256 dt = block.timestamp - lastAccrualTs;
            if (dt > 0) {
                uint256 feeAssets = totalNavAsset * mgmtFeeBps * dt / (10_000 * YEAR);
                if (feeAssets >= totalNavAsset) {
                    // pathological (years unaccrued): cap and advance so the
                    // dilution denominator can't go <= 0
                    lastAccrualTs = block.timestamp;
                } else if (feeAssets > 0) {
                    uint256 feeShares = feeAssets * totalSupply / (totalNavAsset - feeAssets);
                    if (feeShares > 0) {
                        _mint(treasury, feeShares);
                        emit MgmtFeeAccrued(feeShares, feeAssets);
                        lastAccrualTs = block.timestamp; // advance ONLY when charged
                    }
                }
                // feeAssets rounds to dust: leave lastAccrualTs so no time is lost
            }
        }

        // 2. performance fee — gains above the high-water mark
        if (perfFeeBps == 0 || treasury == address(0)) return;
        uint256 pps = _pps();
        if (hwm == 0) {
            hwm = pps;
            return;
        }
        if (pps <= hwm) return;
        uint256 gainAssets = (pps - hwm) * totalSupply / WAD;
        uint256 perfAssets = gainAssets * perfFeeBps / 10_000;
        if (perfAssets == 0 || perfAssets >= totalNavAsset) {
            hwm = pps;
            return;
        }
        uint256 perfShares = perfAssets * totalSupply / (totalNavAsset - perfAssets);
        if (perfShares > 0) {
            _mint(treasury, perfShares);
            emit PerfFeeCrystallized(perfShares, perfAssets);
        }
        hwm = pps;
    }

    // ---------------------------------------------------------------- views

    function heldTokensLength() external view returns (uint256) {
        return heldTokens.length;
    }

    function pricePerShare() external view returns (uint256) {
        return _pps();
    }

    function homeNav() external view returns (uint256) {
        return totalNavAsset > awayNav ? totalNavAsset - awayNav : 0;
    }

    function destination(uint256 chainId)
        external
        view
        returns (bytes memory receiver, bytes memory takeToken, uint256 capBps, uint256 fundedAsset, bool set)
    {
        Destination storage dd = dest[chainId];
        return (dd.receiver, dd.takeToken, dd.capBps, dd.fundedAsset, dd.set);
    }

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
