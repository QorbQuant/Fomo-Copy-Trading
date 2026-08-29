// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ICopyRouter} from "./CopyVault.sol";
import {IERC20} from "./MiniERC20.sol";

interface ISwapRouter02 {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

/// Mainnet execution adapter: routes CopyVault trades through Uniswap V3
/// (SwapRouter02) along keeper-configured paths. The vault only knows the
/// ICopyRouter interface; this adapter pins WHERE liquidity is sourced.
/// Paths are set per (tokenIn, tokenOut) by the owner and validated to start
/// and end with the right tokens, so a bad path cannot reroute a trade.
contract UniswapV3Adapter is ICopyRouter {
    address public owner;
    ISwapRouter02 public immutable swapRouter;
    mapping(address => mapping(address => bytes)) public paths;

    error NotOwner();
    error NoPath(address tokenIn, address tokenOut);
    error BadPath();

    constructor(ISwapRouter02 swapRouter_) {
        owner = msg.sender;
        swapRouter = swapRouter_;
    }

    /// path = abi.encodePacked(tokenIn, fee, [mid, fee,] tokenOut)
    function setPath(address tokenIn, address tokenOut, bytes calldata path) external {
        if (msg.sender != owner) revert NotOwner();
        if (path.length < 43 || (path.length - 20) % 23 != 0) revert BadPath();
        if (_firstToken(path) != tokenIn || _lastToken(path) != tokenOut) revert BadPath();
        paths[tokenIn][tokenOut] = path;
    }

    function swap(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, address to)
        external
        returns (uint256 amountOut)
    {
        bytes memory path = paths[tokenIn][tokenOut];
        if (path.length == 0) revert NoPath(tokenIn, tokenOut);
        require(IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "pull");
        IERC20(tokenIn).approve(address(swapRouter), amountIn);
        amountOut = swapRouter.exactInput(
            ISwapRouter02.ExactInputParams({
                path: path,
                recipient: to,
                amountIn: amountIn,
                amountOutMinimum: minOut
            })
        );
    }

    function _firstToken(bytes calldata path) internal pure returns (address a) {
        a = address(bytes20(path[:20]));
    }

    function _lastToken(bytes calldata path) internal pure returns (address a) {
        a = address(bytes20(path[path.length - 20:]));
    }
}
