// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {MockUSDC} from "../src/MockUSDC.sol";
import {RiskLayerRegistry as Reg} from "../src/RiskLayerRegistry.sol";
import {AgentVault} from "../src/AgentVault.sol";
import {SimCedent} from "../src/SimCedent.sol";
import {Settlement} from "../src/Settlement.sol";

/// Mirrors engine/test_settlement.py. Each test encodes a failure mode that
/// passes silently if implemented the obvious way.
contract SettlementTest is Test {
    MockUSDC usdc;
    Reg registry;
    AgentVault vault;
    SimCedent cedent;
    Settlement settlement;

    address keeper = address(0xBEEF);
    address depositor = address(0xCAFE);

    uint256 constant SCALE = 1e9;          // per notional unit
    uint256 constant SEED = 3_200_000e6;   // vault seed capital
    uint256 constant CEDENT_FUND = 50_000_000e6;

    function setUp() public {
        usdc = new MockUSDC();
        registry = new Reg(bytes32(uint256(1)));
        vault = new AgentVault(usdc, registry);
        cedent = new SimCedent(usdc);
        settlement = new Settlement(usdc, registry, vault, cedent);

        registry.setSettlement(address(settlement));
        vault.setSettlement(address(settlement));
        cedent.setSettlement(address(settlement));
        settlement.setKeeper(keeper);

        // FL_WIND_JUNIOR: attach 576, exhaust 1301, limit 725, ROL 1182 bps
        registry.addLayer(0, 0, 0, 576, 1301, 738, 1182, 0);
        // FL_WIND_MEZZ
        registry.addLayer(0, 0, 1, 1301, 2280, 243, 535, 1);
        // EU_WIND_JUNIOR, different peril
        registry.addLayer(2, 1, 0, 290, 565, 750, 1200, 2);

        usdc.mint(address(cedent), CEDENT_FUND);
        usdc.mint(depositor, SEED);

        vm.startPrank(depositor);
        usdc.approve(address(vault), SEED);
        vault.deposit(SEED, depositor);
        vm.stopPrank();
    }

    function _commit(uint256 layerId, uint256 lineBps) internal {
        uint256[] memory ids = new uint256[](1);
        uint256[] memory lines = new uint256[](1);
        ids[0] = layerId;
        lines[0] = lineBps;
        vm.prank(keeper);
        settlement.renew(ids, lines);
    }

    function _settle(uint8 peril, uint256 lossNotional) internal {
        uint8[] memory p = new uint8[](1);
        uint256[] memory s = new uint256[](1);
        p[0] = peril;
        s[0] = lossNotional * SCALE;
        vm.prank(keeper);
        settlement.settleEpoch(p, s);
    }

    function _settleEmpty() internal {
        vm.prank(keeper);
        settlement.settleEpoch(new uint8[](0), new uint256[](0));
    }

    // --- step 1: premium ---------------------------------------------------

    function test_PremiumScalesByLineAndQuarter() public {
        _commit(0, 5_000);   // 50% line on limit 725
        _settleEmpty();
        // 725e9 * 5000 * 1182 / (1e4 * 1e4 * 4) = 10,712,812,500
        Reg.Layer memory l = registry.get(0);
        assertEq(l.accruedPremium, 10_711_875_000);
    }

    function test_ExhaustedLayerAccruesNothing() public {
        _commit(0, 5_000);
        _settle(0, 9_000);                  // wipes it
        Reg.Layer memory a = registry.get(0);
        assertEq(uint8(a.state), uint8(Reg.LayerState.EXHAUSTED));
        uint256 before = a.accruedPremium;
        _settleEmpty();
        assertEq(registry.get(0).accruedPremium, before);
    }

    // --- step 3: losses ----------------------------------------------------

    function test_PayoutScalesByLinePercent() public {
        _commit(0, 5_000);                  // collateral = 725*0.5 = 362.5
        _settle(0, 1_000);                  // gross = 1000-576 = 424, line -> 212
        assertEq(registry.get(0).collateralRemaining, (362_500_000_000 - 212_000_000_000));
    }

    function test_GrossClampedAtLimit() public {
        _commit(0, 5_000);
        _settle(0, 99_999);
        assertEq(registry.get(0).collateralRemaining, 0);
        assertEq(uint8(registry.get(0).state), uint8(Reg.LayerState.EXHAUSTED));
    }

    function test_ClampIsOnRemainingNotLimit() public {
        _commit(0, 5_000);
        _settle(0, 1_200);                  // gross 624 -> 312 of 362.5
        assertEq(registry.get(0).collateralRemaining, 50_500_000_000);
        _settle(0, 1_200);                  // would be 312 again, only 50.5 left
        assertEq(registry.get(0).collateralRemaining, 0);
    }

    function test_BelowAttachmentNoPayout() public {
        _commit(0, 5_000);
        _settle(0, 500);
        assertEq(registry.get(0).collateralRemaining, 362_500_000_000);
    }

    function test_WrongPerilUntouched() public {
        _commit(0, 5_000);
        _settle(2, 99_999);                 // EU_WIND event, FL layer
        assertEq(registry.get(0).collateralRemaining, 362_500_000_000);
    }

    // --- steps 5 and 6 -----------------------------------------------------

    function test_PremiumAccruesBeforeLossesInSameEpoch() public {
        _commit(0, 5_000);
        _settle(0, 99_999);                 // wiped, but premium still earned
        assertEq(registry.get(0).accruedPremium, 10_711_875_000);
    }

    function test_ExhaustedLayerCountsInNavUntilExpiry() public {
        uint256 navBefore = vault.totalAssets();
        _commit(0, 5_000);
        _settle(0, 99_999);
        // lost 362.5 of collateral, gained premium
        assertEq(vault.totalAssets(), navBefore - 362_500_000_000 + 10_711_875_000);
    }

    function test_ConservationAcrossEpochs() public {
        uint256 opening = vault.totalAssets() + usdc.balanceOf(address(cedent));
        _commit(0, 5_000);
        _commit(2, 4_000);
        for (uint256 i = 0; i < 6; i++) {
            if (i % 2 == 0) _settle(0, 800 + i * 100);
            else _settleEmpty();
        }
        uint256 closing = vault.totalAssets()
            + usdc.balanceOf(address(cedent))
            + usdc.balanceOf(address(settlement))
            - _committedCollateral()
            - _accruedPremium();
        assertApproxEqAbs(closing, opening, 10);
    }

    function _committedCollateral() internal view returns (uint256 t) {
        for (uint256 i = 0; i < registry.layerCount(); i++) {
            t += registry.get(i).collateralRemaining;
        }
    }

    function _accruedPremium() internal view returns (uint256 t) {
        for (uint256 i = 0; i < registry.layerCount(); i++) {
            t += registry.get(i).accruedPremium;
        }
    }
}
