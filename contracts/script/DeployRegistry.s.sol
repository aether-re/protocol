// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {RiskLayerRegistry} from "../src/RiskLayerRegistry.sol";

/// Layers from layer_table.json, 100,000-year calibration.
/// param hash 0x6988eb75f0204da500c0e360b73e1521851d6d811d6e30afafc96acc42b3ec6f
contract DeployRegistry is Script {
    bytes32 constant PARAM_HASH =
        0x6988eb75f0204da500c0e360b73e1521851d6d811d6e30afafc96acc42b3ec6f;

    function run() external {
        vm.startBroadcast();
        RiskLayerRegistry reg = new RiskLayerRegistry(PARAM_HASH);

        // peril, region, tranche, attach, exhaust, EL bps, ROL bps, term offset
        reg.addLayer(0, 0, 0,  576, 1301, 738, 1182, 0);  // FL_WIND_JUNIOR
        reg.addLayer(0, 0, 1, 1301, 2280, 243,  535, 1);  // FL_WIND_MEZZ
        reg.addLayer(0, 0, 2, 2280, 4165,  74,  260, 2);  // FL_WIND_SENIOR
        reg.addLayer(1, 0, 0,  412,  928, 739, 1182, 1);  // GULF_WIND_JUNIOR
        reg.addLayer(1, 0, 1,  928, 1628, 246,  542, 2);  // GULF_WIND_MEZZ
        reg.addLayer(1, 0, 2, 1628, 2944,  73,  256, 3);  // GULF_WIND_SENIOR
        reg.addLayer(2, 1, 0,  290,  565, 750, 1200, 2);  // EU_WIND_JUNIOR
        reg.addLayer(2, 1, 1,  565,  899, 252,  554, 3);  // EU_WIND_MEZZ
        reg.addLayer(2, 1, 2,  899, 1476,  75,  262, 0);  // EU_WIND_SENIOR

        console.log("RiskLayerRegistry:", address(reg));
        console.log("layerCount:", reg.layerCount());
        vm.stopBroadcast();
    }
}
