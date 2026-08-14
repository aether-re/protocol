// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title RiskLayerRegistry
/// @notice The nine catastrophe risk layers and their lifecycle state.
/// Attachment and exhaustion points come from a 100,000-year Monte Carlo
/// calibration; see layer_table.json and its committed parameter hash.
///
/// Amounts are in USDC base units (6 decimals). Notional loss figures are
/// scaled 1:1000, so 1 notional unit ($1M subject loss) == 1,000 USDC == 1e9.
contract RiskLayerRegistry {
    uint256 public constant SCALE = 1e9;   // per notional unit
    uint256 public constant BPS = 10_000;

    /// @dev Three states, never one flag. An EXHAUSTED layer stops accruing
    /// premium but still holds accruedPremium owed back to the vault at term
    /// end, and still counts toward NAV until then.
    enum LayerState { ACTIVE, EXHAUSTED, EXPIRED }

    struct Layer {
        uint8 peril;
        uint8 region;
        uint8 tranche;              // 0 JUNIOR, 1 MEZZ, 2 SENIOR
        uint256 attachment;         // USDC base units
        uint256 exhaustion;
        uint256 technicalELBps;     // calibrated expected loss
        uint256 rateOnLine;         // annual, bps of limit
        uint256 linePercent;        // vault's share, bps
        uint256 collateralPosted;
        uint256 collateralRemaining;
        uint256 accruedPremium;
        uint64 termStart;
        uint64 termEnd;
        LayerState state;
    }

    address public immutable admin;
    address public settlement;
    bytes32 public immutable paramHash;

    uint256 public layerCount;
    mapping(uint256 => Layer) private layers;

    event SettlementSet(address indexed settlement);
    event LayerAdded(uint256 indexed layerId, uint8 peril, uint8 tranche);
    event LayerUpdated(uint256 indexed layerId, LayerState state, uint256 collateralRemaining);

    error NotAdmin();
    error NotSettlement();
    error BadLayer();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlySettlement() {
        if (msg.sender != settlement) revert NotSettlement();
        _;
    }

    constructor(bytes32 _paramHash) {
        admin = msg.sender;
        paramHash = _paramHash;
    }

    function setSettlement(address _settlement) external onlyAdmin {
        settlement = _settlement;
        emit SettlementSet(_settlement);
    }

    /// @notice Adds a calibrated layer. Attachment and exhaustion are given in
    /// notional units and scaled on the way in.
    function addLayer(
        uint8 peril,
        uint8 region,
        uint8 tranche,
        uint256 attachmentNotional,
        uint256 exhaustionNotional,
        uint256 technicalELBps,
        uint256 rateOnLine,
        uint64 termStartOffset
    ) external onlyAdmin returns (uint256 layerId) {
        if (exhaustionNotional <= attachmentNotional) revert BadLayer();

        layerId = layerCount++;
        Layer storage l = layers[layerId];
        l.peril = peril;
        l.region = region;
        l.tranche = tranche;
        l.attachment = attachmentNotional * SCALE;
        l.exhaustion = exhaustionNotional * SCALE;
        l.technicalELBps = technicalELBps;
        l.rateOnLine = rateOnLine;
        l.termEnd = termStartOffset;   // staggered genesis
        l.state = LayerState.EXPIRED;  // uncommitted until first renewal

        emit LayerAdded(layerId, peril, tranche);
    }

    function get(uint256 layerId) external view returns (Layer memory) {
        return layers[layerId];
    }

    function limit(uint256 layerId) public view returns (uint256) {
        Layer storage l = layers[layerId];
        return l.exhaustion - l.attachment;
    }

    /// @notice collateralRemaining + accruedPremium (waterfall step 4).
    function layerValue(uint256 layerId) external view returns (uint256) {
        Layer storage l = layers[layerId];
        return l.collateralRemaining + l.accruedPremium;
    }

    // --- settlement-only mutators -----------------------------------------

    function addPremium(uint256 layerId, uint256 amount) external onlySettlement {
        layers[layerId].accruedPremium += amount;
    }

    function applyLoss(uint256 layerId, uint256 payout) external onlySettlement {
        Layer storage l = layers[layerId];
        l.collateralRemaining -= payout;
        if (l.collateralRemaining == 0) l.state = LayerState.EXHAUSTED;
        emit LayerUpdated(layerId, l.state, l.collateralRemaining);
    }

    function expire(uint256 layerId) external onlySettlement returns (uint256 released) {
        Layer storage l = layers[layerId];
        released = l.collateralRemaining + l.accruedPremium;
        l.collateralRemaining = 0;
        l.accruedPremium = 0;
        l.linePercent = 0;
        l.collateralPosted = 0;
        l.state = LayerState.EXPIRED;
        emit LayerUpdated(layerId, l.state, 0);
    }

    function commit(
        uint256 layerId,
        uint256 linePercent,
        uint256 collateral,
        uint256 rateOnLine,
        uint64 termStart,
        uint64 termEnd
    ) external onlySettlement {
        Layer storage l = layers[layerId];
        l.linePercent = linePercent;
        l.collateralPosted = collateral;
        l.collateralRemaining = collateral;
        l.accruedPremium = 0;
        l.rateOnLine = rateOnLine;
        l.termStart = termStart;
        l.termEnd = termEnd;
        l.state = LayerState.ACTIVE;
        emit LayerUpdated(layerId, l.state, collateral);
    }
}
