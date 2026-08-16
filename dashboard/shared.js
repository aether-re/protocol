// Aether Re: wallet, contracts and shared formatting for the position and
// deposit pages. Both pages import this so the connection logic exists once.
import { ethers } from "https://cdn.jsdelivr.net/npm/ethers@6.13.4/+esm";

export const USDC  = "0x82B7CE5992F87Ae64537d708d39Bd233B7aA7cfb";
export const VAULT = "0xDC78557b332B1AF7e157ab91D34f432F30481a53";

const CHAIN = {
  chainId:"0x7A0", chainName:"X Layer Testnet",
  nativeCurrency:{name:"OKB",symbol:"OKB",decimals:18},
  rpcUrls:["https://testrpc.xlayer.tech/terigon"],
  blockExplorerUrls:["https://www.okx.com/web3/explorer/xlayer-test"],
};
const ERC20 = ["function balanceOf(address) view returns (uint256)",
  "function allowance(address,address) view returns (uint256)",
  "function approve(address,uint256) returns (bool)","function faucet()"];
const VAULT_ABI = ["function balanceOf(address) view returns (uint256)",
  "function totalAssets() view returns (uint256)","function idleAssets() view returns (uint256)",
  "function convertToAssets(uint256) view returns (uint256)",
  "function deposit(uint256,address) returns (uint256)",
  "function withdraw(uint256,address,address) returns (uint256)"];

export const $ = s => document.querySelector(s);
export const U = 1_000_000n;
export const fmt = (v,d=0) =>
  (Number(v)/1e6).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
export const parseAmt = s => {
  const [a,b=""] = String(s).trim().split(".");
  if(!/^\d*$/.test(a) || !/^\d*$/.test(b)) throw new Error("bad");
  return BigInt(a||"0")*U + BigInt((b+"000000").slice(0,6));
};
export { ethers };

export const wallet = {
  provider:null, signer:null, account:null, usdc:null, vault:null,

  async connect(){
    if(!window.ethereum) throw new Error("NO_WALLET");
    await window.ethereum.request({method:"eth_requestAccounts"});
    const id = await window.ethereum.request({method:"eth_chainId"});
    if(id !== CHAIN.chainId){
      try{
        await window.ethereum.request({method:"wallet_switchEthereumChain",
          params:[{chainId:CHAIN.chainId}]});
      }catch(e){
        if(e.code === 4902)
          await window.ethereum.request({method:"wallet_addEthereumChain", params:[CHAIN]});
        else throw e;
      }
    }
    this.provider = new ethers.BrowserProvider(window.ethereum);
    this.signer   = await this.provider.getSigner();
    this.account  = await this.signer.getAddress();
    this.usdc  = new ethers.Contract(USDC,  ERC20,     this.signer);
    this.vault = new ethers.Contract(VAULT, VAULT_ABI, this.signer);
    sessionStorage.setItem("aether.connected","1");
    return this.account;
  },

  // Reconnect silently if the wallet is already authorised, so moving between
  // pages does not make the person click connect again.
  async resume(){
    if(!window.ethereum || sessionStorage.getItem("aether.connected") !== "1") return null;
    const accts = await window.ethereum.request({method:"eth_accounts"});
    if(!accts?.length) return null;
    return this.connect();
  },

  disconnect(){
    sessionStorage.removeItem("aether.connected");
    this.provider = this.signer = this.account = this.usdc = this.vault = null;
  },

  short(){ return this.account ? this.account.slice(0,6)+"…"+this.account.slice(-4) : ""; },

  async snapshot(){
    const a = this.account;
    const [bal, shares, nav, idle, okb] = await Promise.all([
      this.usdc.balanceOf(a), this.vault.balanceOf(a),
      this.vault.totalAssets(), this.vault.idleAssets(), this.provider.getBalance(a),
    ]);
    const value = shares > 0n ? await this.vault.convertToAssets(shares) : 0n;
    const redeemable = value < idle ? value : idle;
    return {bal, shares, nav, idle, okb, value, redeemable};
  },
};

export function explain(e){
  const m = e?.info?.error?.message || e?.shortMessage || e?.message || String(e);
  if(m === "NO_WALLET") return "No wallet found. Install OKX Wallet or another injected wallet, then reload.";
  if(/user rejected|denied/i.test(m)) return "Transaction rejected in wallet.";
  if(/insufficient funds/i.test(m))   return "Not enough OKB for gas. Use the X Layer faucet.";
  return m.length > 220 ? m.slice(0,220)+"…" : m;
}

export function say(text, kind=""){
  const el = $("#msg");
  if(el) el.innerHTML = text ? `<div class="msg ${kind}">${text}</div>` : "";
}

export async function run(label, fn, after){
  const btns = [...document.querySelectorAll("button.act")];
  const was = btns.map(b => b.disabled);
  btns.forEach(b => b.disabled = true);
  let ok = false;
  try{
    say(`${label}, confirm in your wallet…`);
    const tx = await fn();
    say(`${label}, waiting for confirmation…`);
    await tx.wait();
    say(`${label} confirmed.`,"ok");
    ok = true;
    if(after) await after();
  }catch(e){ say(explain(e),"err"); }
  btns.forEach((b,i) => b.disabled = was[i]);
  return ok;
}
