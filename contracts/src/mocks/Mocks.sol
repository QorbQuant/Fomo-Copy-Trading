// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MiniERC20, IERC20} from "../MiniERC20.sol";
import {ICopyRouter} from "../CopyVault.sol";
import {IDlnSource, OrderCreation} from "../IDlnSource.sol";

contract MockERC20 is MiniERC20 {
    constructor(string memory name_, string memory symbol_, uint8 decimals_)
        MiniERC20(name_, symbol_, decimals_)
    {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

/// Records DLN orders for assertions; charges a fixed native fee like the
/// real DlnSource and pulls the give amount.
contract MockDlnSource is IDlnSource {
    uint88 public constant FEE = 0.001 ether;
    OrderCreation public lastOrder;
    uint256 public orders;

    function globalFixedNativeFee() external pure returns (uint88) {
        return FEE;
    }

    function createOrder(OrderCreation calldata o, bytes calldata, uint32, bytes calldata)
        external
        payable
        returns (bytes32)
    {
        require(msg.value == FEE, "fee");
        require(IERC20(o.giveTokenAddress).transferFrom(msg.sender, address(this), o.giveAmount), "give");
        lastOrder = o;
        orders++;
        return keccak256(abi.encode(o, orders));
    }
}

/// Fixed-rate mock DEX: pays out amountIn * rate / 1e18 of tokenOut from its
/// own reserves. Rates are set per (tokenIn, tokenOut) pair.
contract MockRouter is ICopyRouter {
    mapping(address => mapping(address => uint256)) public rate; // 1e18 = 1:1

    function setRate(address tokenIn, address tokenOut, uint256 rateWad) external {
        rate[tokenIn][tokenOut] = rateWad;
    }

    function swap(address tokenIn, address tokenOut, uint256 amountIn, uint256, address to)
        external
        returns (uint256 amountOut)
    {
        require(rate[tokenIn][tokenOut] > 0, "no market");
        require(IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "in");
        amountOut = amountIn * rate[tokenIn][tokenOut] / 1e18;
        require(IERC20(tokenOut).transfer(to, amountOut), "out");
    }
}

/// Minimal WETH: deposit() wraps native into 1:1 tokens; withdraw() unwraps
/// and returns native. Used to exercise CopyVaultV3.sweepGas end-to-end.
contract MockWETH is MiniERC20 {
    constructor() MiniERC20("Wrapped Ether", "WETH", 18) {}

    function deposit() external payable {
        _mint(msg.sender, msg.value);
    }

    function withdraw(uint256 amount) external {
        _burn(msg.sender, amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok, "eth");
    }

    receive() external payable {
        _mint(msg.sender, msg.value);
    }
}
