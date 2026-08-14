// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {RiskLayerRegistry} from "./RiskLayerRegistry.sol";

/// @title AgentVault
/// @notice ERC-4626 vault and the single source of truth for NAV.
///
/// NAV = idle USDC + sum of (collateralRemaining + accruedPremium) across all
/// layers that have not expired. Exhausted-but-unexpired layers still count:
/// their accrued premium is real and returns to the vault at term end.
///
/// Capital committed to a live layer cannot be withdrawn mid-term, so
/// redemptions are served from idle balance only. A queue is layered on top
/// of this in a later contract; the vault never pretends to instant liquidity
/// it does not have.
contract AgentVault is ERC4626 {
    using SafeERC20 for IERC20;

    RiskLayerRegistry public immutable registry;
    address public immutable admin;
    address public settlement;

    /// @dev Capital handed to settlement for layer collateral. Tracked so
    /// totalAssets can distinguish "deployed" from "lost".
    uint256 public committed;

    event SettlementSet(address indexed settlement);
    event CapitalCommitted(uint256 amount);
    event CapitalReleased(uint256 amount);

    error NotAdmin();
    error NotSettlement();
    error InsufficientIdle();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlySettlement() {
        if (msg.sender != settlement) revert NotSettlement();
        _;
    }

    constructor(IERC20 _usdc, RiskLayerRegistry _registry)
        ERC20("Aether Re Vault", "aRE")
        ERC4626(_usdc)
    {
        registry = _registry;
        admin = msg.sender;
    }

    function setSettlement(address _settlement) external onlyAdmin {
        settlement = _settlement;
        emit SettlementSet(_settlement);
    }

    /// @notice Idle USDC held directly by the vault.
    function idleAssets() public view returns (uint256) {
        return IERC20(asset()).balanceOf(address(this));
    }

    /// @notice NAV. Waterfall step 6.
    function totalAssets() public view override returns (uint256) {
        uint256 nav = idleAssets();
        uint256 n = registry.layerCount();
        for (uint256 i = 0; i < n; i++) {
            RiskLayerRegistry.Layer memory l = registry.get(i);
            if (l.state != RiskLayerRegistry.LayerState.EXPIRED) {
                nav += l.collateralRemaining + l.accruedPremium;
            }
        }
        return nav;
    }

    /// @notice Idle capital that could be committed without breaching a buffer.
    function deployable(uint256 minBufferBps) external view returns (uint256) {
        uint256 buffer = (totalAssets() * minBufferBps) / 10_000;
        uint256 idle = idleAssets();
        return idle > buffer ? idle - buffer : 0;
    }

    // --- settlement-only capital movement ---------------------------------

    /// @notice Sends collateral out to back a layer commitment.
    function commitCapital(address to, uint256 amount) external onlySettlement {
        if (amount > idleAssets()) revert InsufficientIdle();
        committed += amount;
        IERC20(asset()).safeTransfer(to, amount);
        emit CapitalCommitted(amount);
    }

    /// @notice Called after released capital has been transferred back in.
    function recordRelease(uint256 amount) external onlySettlement {
        committed = amount > committed ? 0 : committed - amount;
        emit CapitalReleased(amount);
    }
}
