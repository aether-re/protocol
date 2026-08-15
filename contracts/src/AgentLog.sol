// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title AgentLog
/// @notice The agent's published loss forecasts and their resolution.
///
/// A forecast is only falsifiable if it is timestamped before the outcome is
/// known. Every forecast here is written in the epoch *before* the one it
/// predicts, and resolved afterwards from settled losses. The operator cannot
/// revise a forecast: publish reverts if one already exists for that epoch,
/// and resolve reverts if already resolved.
///
/// Loss figures are basis points of NAV at the start of the forecast epoch,
/// so predicted and realised are directly comparable.
contract AgentLog {
    struct Forecast {
        uint32 expectedLossBps;
        uint16 confidenceBps;
        uint32 realizedLossBps;
        bool published;
        bool resolved;
        string rationale;
    }

    address public immutable admin;
    address public keeper;

    mapping(uint64 => Forecast) public forecasts;
    uint64 public firstEpoch;
    uint64 public lastPublished;
    uint64 public resolvedCount;

    /// @dev Running totals for a calibration score without off-chain replay.
    uint256 public sumPredictedBps;
    uint256 public sumRealizedBps;

    event ForecastPublished(uint64 indexed epoch, uint32 expectedLossBps, uint16 confidenceBps, string rationale);
    event ForecastResolved(uint64 indexed epoch, uint32 expectedLossBps, uint32 realizedLossBps);
    event KeeperSet(address indexed keeper);

    error NotAdmin();
    error NotKeeper();
    error AlreadyPublished();
    error NotPublished();
    error AlreadyResolved();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlyKeeper() {
        if (msg.sender != keeper) revert NotKeeper();
        _;
    }

    constructor() {
        admin = msg.sender;
    }

    function setKeeper(address _keeper) external onlyAdmin {
        keeper = _keeper;
        emit KeeperSet(_keeper);
    }

    /// @notice Publishes a forecast for a future epoch. One shot per epoch.
    function publishForecast(
        uint64 epoch,
        uint32 expectedLossBps,
        uint16 confidenceBps,
        string calldata rationale
    ) external onlyKeeper {
        Forecast storage f = forecasts[epoch];
        if (f.published) revert AlreadyPublished();

        f.expectedLossBps = expectedLossBps;
        f.confidenceBps = confidenceBps;
        f.rationale = rationale;
        f.published = true;

        if (lastPublished == 0 && firstEpoch == 0) firstEpoch = epoch;
        if (epoch > lastPublished) lastPublished = epoch;

        sumPredictedBps += expectedLossBps;
        emit ForecastPublished(epoch, expectedLossBps, confidenceBps, rationale);
    }

    /// @notice Records what actually happened. Cannot be revised.
    function resolveForecast(uint64 epoch, uint32 realizedLossBps) external onlyKeeper {
        Forecast storage f = forecasts[epoch];
        if (!f.published) revert NotPublished();
        if (f.resolved) revert AlreadyResolved();

        f.realizedLossBps = realizedLossBps;
        f.resolved = true;
        resolvedCount++;
        sumRealizedBps += realizedLossBps;

        emit ForecastResolved(epoch, f.expectedLossBps, realizedLossBps);
    }

    function get(uint64 epoch) external view returns (Forecast memory) {
        return forecasts[epoch];
    }

    /// @notice Predicted over realised, in bps. 10000 means perfectly
    /// calibrated in aggregate; above means the agent over-predicted losses.
    function calibrationRatioBps() external view returns (uint256) {
        if (sumRealizedBps == 0) return 0;
        return (sumPredictedBps * 10_000) / sumRealizedBps;
    }
}
