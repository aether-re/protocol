// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {EventOracle} from "../src/EventOracle.sol";

contract DeployOracle is Script {
    bytes32 constant PARAM_HASH =
        0x6988eb75f0204da500c0e360b73e1521851d6d811d6e30afafc96acc42b3ec6f;

    /// keccak256 of a secret 32-byte seed, committed before any event is
    /// published. Revealed at run end so anyone can replay the event stream
    /// and confirm nothing was tuned.
    bytes32 constant SEED_COMMITMENT =
        0x097c38be17afb6d93d776fc460c702c96b48f775bd1dcf8c8170093225c3a458;

    function run() external {
        vm.startBroadcast();
        EventOracle oracle = new EventOracle(SEED_COMMITMENT, PARAM_HASH);
        oracle.setKeeper(msg.sender);
        console.log("EventOracle:", address(oracle));
        vm.stopBroadcast();
    }
}
