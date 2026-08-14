// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {RiskLayerRegistry as Reg} from "./RiskLayerRegistry.sol";
import {AgentVault} from "./AgentVault.sol";
import {SimCedent} from "./SimCedent.sol";

/// @title Settlement
/// @notice The epoch waterfall. Direct port of engine/settlement.py; the two
/// implementations must agree on every input.
///
/// STEP ORDER IS NOT NEGOTIABLE:
///   1 accrue premium   (ACTIVE only; always before losses)
///   2 draw events      (supplied by keeper from the oracle)
///   3 apply losses     (sequential erosion; clamp on collateralRemaining)
///   4 mark values      (implicit: collateralRemaining + accruedPremium)
///   5 expire + release (ACTIVE *and* EXHAUSTED at termEnd)
///   6 compute NAV      (vault.totalAssets)
///   7 agent decides    (off-chain)
///   8 renew            (separate call)
contract Settlement {
    using SafeERC20 for IERC20;

    uint256 public constant BPS = 10_000;
    uint256 public constant EPOCHS_PER_YEAR = 4;
    uint64 public constant TERM_EPOCHS = 4;

    IERC20 public immutable usdc;
    Reg public immutable registry;
    AgentVault public immutable vault;
    SimCedent public immutable cedent;

    address public immutable admin;
    address public keeper;
    uint64 public epoch;
    bool public paused;

    event EpochSettled(uint64 indexed epoch, uint256 premium, uint256 losses, uint256 released, uint256 nav);
    event LayerCommitted(uint64 indexed epoch, uint256 indexed layerId, uint256 linePercent, uint256 collateral);
    event Paused(bool paused);

    error NotAdmin();
    error NotKeeper();
    error IsPaused();
    error LengthMismatch();
    error NotRenewable();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlyKeeper() {
        if (msg.sender != keeper) revert NotKeeper();
        _;
    }

    constructor(IERC20 _usdc, Reg _registry, AgentVault _vault, SimCedent _cedent) {
        usdc = _usdc;
        registry = _registry;
        vault = _vault;
        cedent = _cedent;
        admin = msg.sender;
    }

    function setKeeper(address _keeper) external onlyAdmin {
        keeper = _keeper;
    }

    function setPaused(bool p) external onlyAdmin {
        paused = p;
        emit Paused(p);
    }

    /// @notice Steps 1 through 6 for one epoch.
    function settleEpoch(uint8[] calldata perils, uint256[] calldata subjectLosses)
        external
        onlyKeeper
        returns (uint256 nav)
    {
        if (paused) revert IsPaused();
        if (perils.length != subjectLosses.length) revert LengthMismatch();

        uint256 premium = _accruePremium();
        uint256 losses = _applyLosses(perils, subjectLosses);
        uint256 released = _expireAndRelease();

        nav = vault.totalAssets();
        emit EpochSettled(epoch, premium, losses, released, nav);
        epoch++;
    }

    /// @dev Step 1. EXHAUSTED layers accrue nothing: they provide no cover.
    /// Single division so truncation matches the Python reference exactly.
    function _accruePremium() internal returns (uint256 total) {
        uint256 n = registry.layerCount();
        for (uint256 i = 0; i < n; i++) {
            Reg.Layer memory l = registry.get(i);
            if (l.state != Reg.LayerState.ACTIVE) continue;

            uint256 lim = l.exhaustion - l.attachment;
            uint256 premium = (lim * l.linePercent * l.rateOnLine) / (BPS * BPS * EPOCHS_PER_YEAR);
            if (premium == 0) continue;

            uint256 paid = cedent.payPremium(address(this), premium);
            registry.addPremium(i, premium);
            total += paid;
        }
    }

    /// @dev Step 3. Sequential erosion: collateralRemaining is re-read for
    /// every event, so a layer eroded earlier in the term cannot overpay.
    function _applyLosses(uint8[] calldata perils, uint256[] calldata subjectLosses)
        internal
        returns (uint256 total)
    {
        uint256 n = registry.layerCount();
        for (uint256 e = 0; e < perils.length; e++) {
            for (uint256 i = 0; i < n; i++) {
                Reg.Layer memory l = registry.get(i);
                if (l.state != Reg.LayerState.ACTIVE) continue;
                if (l.peril != perils[e]) continue;
                if (subjectLosses[e] <= l.attachment) continue;

                uint256 lim = l.exhaustion - l.attachment;
                uint256 excess = subjectLosses[e] - l.attachment;
                uint256 gross = excess > lim ? lim : excess;

                uint256 payout = (gross * l.linePercent) / BPS;
                if (payout > l.collateralRemaining) payout = l.collateralRemaining;
                if (payout == 0) continue;

                registry.applyLoss(i, payout);
                total += payout;
            }
        }

        if (total > 0) {
            usdc.safeTransfer(address(cedent), total);
            cedent.recordLoss(total);
        }
    }

    /// @dev Step 5. Sweeps ACTIVE and EXHAUSTED alike. An exhausted layer
    /// still holds accruedPremium owed to the vault; skipping it burns that
    /// premium silently.
    function _expireAndRelease() internal returns (uint256 total) {
        uint256 n = registry.layerCount();
        for (uint256 i = 0; i < n; i++) {
            Reg.Layer memory l = registry.get(i);
            if (l.state == Reg.LayerState.EXPIRED) continue;
            if (l.termEnd != epoch) continue;

            uint256 released = registry.expire(i);
            total += released;
        }

        if (total > 0) {
            usdc.safeTransfer(address(vault), total);
            vault.recordRelease(total);
        }
    }

    /// @notice Step 8. Commits idle capital to layers per the agent's target
    /// lines. Constraint checking happens off-chain; this enforces only that
    /// the layer is renewable and the vault has the capital.
    function renew(uint256[] calldata layerIds, uint256[] calldata linePercents)
        external
        onlyKeeper
    {
        if (paused) revert IsPaused();
        if (layerIds.length != linePercents.length) revert LengthMismatch();

        for (uint256 k = 0; k < layerIds.length; k++) {
            uint256 id = layerIds[k];
            Reg.Layer memory l = registry.get(id);
            if (l.state != Reg.LayerState.EXPIRED) revert NotRenewable();

            uint256 lim = l.exhaustion - l.attachment;
            uint256 collateral = (lim * linePercents[k]) / BPS;
            if (collateral == 0) continue;

            vault.commitCapital(address(this), collateral);
            registry.commit(id, linePercents[k], collateral, l.rateOnLine, epoch, epoch + TERM_EPOCHS);

            emit LayerCommitted(epoch, id, linePercents[k], collateral);
        }
    }
}
