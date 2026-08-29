// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MiniERC20, IERC20} from "../MiniERC20.sol";
import {ICopyRouter} from "../CopyVault.sol";

contract MockERC20 is MiniERC20 {
    constructor(string memory name_, string memory symbol_, uint8 decimals_)
        MiniERC20(name_, symbol_, decimals_)
    {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
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
