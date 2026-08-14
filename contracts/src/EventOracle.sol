// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title EventOracle
/// @notice Publishes simulated catastrophe events, under a commitment made
/// before the run begins.
///
/// Both the seed AND the frozen parameter set are committed up front. The seed
/// alone proves nothing: if lambda and severity could be retuned afterwards,
/// the event stream could still be shaped to taste. Committing both means the
/// published replay script reproduces exactly this event sequence or the
/// commitment is broken.
///
/// SIMULATION ARTIFACT. On mainnet this is replaced by a parametric oracle
/// reading NOAA wind speed and USGS magnitude.
contract EventOracle {
    struct CatEvent {
        uint64 epoch;
        uint8 peril;
        uint256 subjectLoss;   // USDC base units, 1e9 per notional unit
    }

    address public immutable admin;
    address public keeper;

    /// @dev Committed before the first event is published.
    bytes32 public immutable seedCommitment;
    bytes32 public immutable paramHash;

    /// @dev Revealed at run end. Zero until then.
    bytes32 public revealedSeed;
    bool public revealed;

    CatEvent[] public events;
    uint64 public lastEpoch;

    event EventPublished(uint256 indexed eventId, uint64 indexed epoch, uint8 peril, uint256 subjectLoss);
    event SeedRevealed(bytes32 seed);
    event KeeperSet(address indexed keeper);

    error NotAdmin();
    error NotKeeper();
    error AlreadyRevealed();
    error BadReveal();
    error EpochWentBackwards();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlyKeeper() {
        if (msg.sender != keeper) revert NotKeeper();
        _;
    }

    constructor(bytes32 _seedCommitment, bytes32 _paramHash) {
        admin = msg.sender;
        seedCommitment = _seedCommitment;
        paramHash = _paramHash;
    }

    function setKeeper(address _keeper) external onlyAdmin {
        keeper = _keeper;
        emit KeeperSet(_keeper);
    }

    /// @notice Publishes the events for an epoch. Epochs are monotonic.
    function publish(uint64 epoch, uint8[] calldata perils, uint256[] calldata subjectLosses)
        external
        onlyKeeper
    {
        if (epoch < lastEpoch) revert EpochWentBackwards();
        lastEpoch = epoch;

        for (uint256 i = 0; i < perils.length; i++) {
            events.push(CatEvent(epoch, perils[i], subjectLosses[i]));
            emit EventPublished(events.length - 1, epoch, perils[i], subjectLosses[i]);
        }
    }

    /// @notice Reveals the seed. Anyone can now replay the full event stream
    /// and check it against what was published here.
    function reveal(bytes32 seed) external onlyAdmin {
        if (revealed) revert AlreadyRevealed();
        if (keccak256(abi.encodePacked(seed)) != seedCommitment) revert BadReveal();
        revealedSeed = seed;
        revealed = true;
        emit SeedRevealed(seed);
    }

    function eventCount() external view returns (uint256) {
        return events.length;
    }

    function eventsForEpoch(uint64 epoch) external view returns (CatEvent[] memory out) {
        uint256 n;
        for (uint256 i = 0; i < events.length; i++) {
            if (events[i].epoch == epoch) n++;
        }
        out = new CatEvent[](n);
        uint256 j;
        for (uint256 i = 0; i < events.length; i++) {
            if (events[i].epoch == epoch) out[j++] = events[i];
        }
    }
}
