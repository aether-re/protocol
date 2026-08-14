// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title SimCedent
/// @notice The simulated insurer buying catastrophe cover. Pays premium into
/// the vault each epoch and claims against layer collateral when events fire.
///
/// This contract is what makes the vault's yield an accounting identity rather
/// than a number chosen by the protocol: every unit of premium comes out of
/// this balance, and every loss goes back into it.
///
/// SIMULATION ARTIFACT. On mainnet this is replaced by a real cover buyer.
contract SimCedent {
    using SafeERC20 for IERC20;

    IERC20 public immutable usdc;
    address public immutable admin;
    address public settlement;

    /// @dev Premium owed but unpayable due to shortfall. The epoch loop must
    /// never halt on an empty cedent -- a stalled keeper loses the whole run.
    uint256 public iou;

    uint256 public totalPremiumPaid;
    uint256 public totalLossesReceived;

    event SettlementSet(address indexed settlement);
    event PremiumPaid(address indexed to, uint256 amount, uint256 shortfall);
    event ToppedUp(uint256 amount, uint256 iouCleared);

    error NotAdmin();
    error NotSettlement();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlySettlement() {
        if (msg.sender != settlement) revert NotSettlement();
        _;
    }

    constructor(IERC20 _usdc) {
        usdc = _usdc;
        admin = msg.sender;
    }

    function setSettlement(address _settlement) external onlyAdmin {
        settlement = _settlement;
        emit SettlementSet(_settlement);
    }

    /// @notice Pays premium to the vault. On shortfall, pays what it can and
    /// books the remainder as an IOU rather than reverting.
    function payPremium(address to, uint256 amount)
        external
        onlySettlement
        returns (uint256 paid)
    {
        uint256 bal = usdc.balanceOf(address(this));
        paid = amount > bal ? bal : amount;
        uint256 shortfall = amount - paid;

        if (shortfall > 0) iou += shortfall;
        if (paid > 0) {
            usdc.safeTransfer(to, paid);
            totalPremiumPaid += paid;
        }

        emit PremiumPaid(to, paid, shortfall);
    }

    /// @notice Called after loss proceeds have been transferred in.
    function recordLoss(uint256 amount) external onlySettlement {
        totalLossesReceived += amount;
    }

    /// @notice Admin refills the cedent and settles any outstanding IOU.
    /// Logged publicly: this is a simulation artifact, not a hidden subsidy.
    function topUp(address to) external onlyAdmin {
        uint256 owed = iou;
        if (owed > 0) {
            uint256 bal = usdc.balanceOf(address(this));
            uint256 pay = owed > bal ? bal : owed;
            iou -= pay;
            if (pay > 0) usdc.safeTransfer(to, pay);
            emit ToppedUp(pay, pay);
        }
    }

    function balance() external view returns (uint256) {
        return usdc.balanceOf(address(this));
    }
}
