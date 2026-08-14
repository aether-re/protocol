// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {MockUSDC} from "../src/MockUSDC.sol";
import {RiskLayerRegistry as Reg} from "../src/RiskLayerRegistry.sol";
import {AgentVault} from "../src/AgentVault.sol";
import {SimCedent} from "../src/SimCedent.sol";
import {Settlement} from "../src/Settlement.sol";
import {EventOracle} from "../src/EventOracle.sol";

contract DeploySystem is Script {
    bytes32 constant PARAM_HASH =
        0x6988eb75f0204da500c0e360b73e1521851d6d811d6e30afafc96acc42b3ec6f;

    // keccak256 of the live seed. Committed BEFORE the run starts.
    bytes32 constant SEED_COMMITMENT =
        0x211de83caed51d11de93b787115ff793e770b2b97fede2faedc71ca19e59ede0;

    function run() external {
        address me = msg.sender;
        vm.startBroadcast();

        MockUSDC usdc = MockUSDC(vm.envAddress("USDC"));
        Reg registry = Reg(vm.envAddress("REGISTRY"));

        AgentVault vault = new AgentVault(usdc, registry);
        SimCedent cedent = new SimCedent(usdc);
        Settlement settlement = new Settlement(usdc, registry, vault, cedent);
        EventOracle oracle = new EventOracle(SEED_COMMITMENT, PARAM_HASH);

        vault.setSettlement(address(settlement));
        cedent.setSettlement(address(settlement));
        settlement.setKeeper(me);
        oracle.setKeeper(me);

        usdc.mint(address(cedent), 50_000_000e6);

        console.log("AgentVault: ", address(vault));
        console.log("SimCedent:  ", address(cedent));
        console.log("Settlement: ", address(settlement));
        console.log("EventOracle:", address(oracle));
        vm.stopBroadcast();
    }
}
