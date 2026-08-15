// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {AgentLog} from "../src/AgentLog.sol";

contract DeployAgentLog is Script {
    function run() external {
        vm.startBroadcast();
        AgentLog log = new AgentLog();
        log.setKeeper(msg.sender);
        console.log("AgentLog:", address(log));
        vm.stopBroadcast();
    }
}
