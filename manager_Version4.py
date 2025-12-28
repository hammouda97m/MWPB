import json
import os
import time
import secrets
from datetime import datetime
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from dotenv import load_dotenv, find_dotenv
from decimal import Decimal
import requests
import threading

# === Config ===
load_dotenv(find_dotenv())

MAIN_PRIVATE_KEY = os.getenv("MAIN_PRIVATE_KEY")
MAIN_WALLET_ADDRESS = os.getenv("MAIN_WALLET_ADDRESS")

PREDICTION_CONTRACT = "0x18B2A687610328590Bc8F2e5fEdDe3b582A49cdA"
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"

with open("prediction_abi.json", "r") as f:
    PREDICTION_ABI = json.load(f)

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForETH",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"}
        ],
        "name": "getAmountsOut",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

web3 = Web3(Web3.HTTPProvider("https://solemn-flashy-surf.bsc.quiknode.pro/3e1ec42374e87ebcf909c51ced78c7948af2d563/"))
web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not web3.is_connected():
    raise Exception("❌ Failed to connect to BSC")

prediction_contract = web3.eth.contract(
    address=Web3.to_checksum_address(PREDICTION_CONTRACT),
    abi=PREDICTION_ABI
)
usdt_contract = web3.eth.contract(
    address=Web3.to_checksum_address(USDT_CONTRACT),
    abi=ERC20_ABI
)
router_contract = web3.eth.contract(
    address=Web3.to_checksum_address(PANCAKE_ROUTER),
    abi=ROUTER_ABI
)

WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

# === TELEGRAM BOT FUNCTIONS ===
last_update_id = 0


def get_telegram_updates():
    """Get new messages from Telegram INSTANTLY"""
    global last_update_id
    try:
        token = os.getenv("TELEGRAM_TOKEN")
        if not token:
            return []

        url = f"https://api.telegram.org/bot{token}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 0}  # NO TIMEOUT = INSTANT
        response = requests.get(url, params=params, timeout=1)

        if response.ok:
            data = response.json()
            updates = data.get('result', [])

            if updates:
                last_update_id = updates[-1]['update_id']

            return updates
    except:
        pass
    return []


def parse_bet_command(message_text):
    """Parse '/bet 1/50/up' into wallet_idx, usdt_amount, direction"""
    try:
        if not message_text.startswith('/bet '):
            return None

        # Remove '/bet ' and split
        cmd_part = message_text.replace('/bet ', '').strip()
        parts = cmd_part.split('/')

        if len(parts) != 3:
            return None

        wallet_idx = int(parts[0]) - 1  # Convert to 0-based
        usdt_amount = float(parts[1])
        direction = parts[2].lower()

        if direction not in ['up', 'down']:
            return None

        return {
            'wallet_idx': wallet_idx,
            'usdt_amount': usdt_amount,
            'direction': direction
        }
    except:
        return None


def execute_telegram_bet(cmd, wallet_manager, swap_manager, betting_manager):
    """Execute bet from Telegram command"""
    try:
        # Validate wallet
        if cmd['wallet_idx'] < 0 or cmd['wallet_idx'] >= len(wallet_manager.wallets):
            send_telegram_message("❌ Invalid wallet number!")
            return False

        if cmd['usdt_amount'] <= 0:
            send_telegram_message("❌ Invalid USDT amount!")
            return False

        selected_wallet = wallet_manager.wallets[cmd['wallet_idx']]

        # Show preview
        expected_bnb = swap_manager.get_usdt_to_bnb_rate(cmd['usdt_amount'])

        preview_msg = (
            f"⚡ INSTANT TELEGRAM BET!\n\n"
            f"💱 {cmd['usdt_amount']} USDT → ~{expected_bnb:.6f} BNB\n"
            f"👤 Wallet: {selected_wallet['name']}\n"
            f"🎯 Direction: {cmd['direction'].upper()}\n"
            f"⏳ Processing INSTANTLY..."
        )
        send_telegram_message(preview_msg)

        # Execute swap
        success = swap_manager.swap_usdt_to_bnb(
            cmd['usdt_amount'],
            selected_wallet['address']
        )

        if not success:
            send_telegram_message("❌ Swap failed!")
            return False

        send_telegram_message("✅ Swap completed! Placing bet...")
        time.sleep(0.5)

        # Get updated balance and place bet
        selected_wallet = wallet_manager.get_wallet_balances(selected_wallet)
        bet_amount = selected_wallet['balance_bnb'] * 0.95  # Use 95% for gas

        betting_success = betting_manager.place_bet(
            selected_wallet,
            cmd['direction'],
            bet_amount
        )

        if betting_success:
            success_msg = (
                f"🎯 BET PLACED INSTANTLY!\n\n"
                f"💱 Swapped: {cmd['usdt_amount']} USDT\n"
                f"🎲 Bet: {cmd['direction'].upper()} with {bet_amount:.6f} BNB\n"
                f"👤 Wallet: {selected_wallet['name']}\n"
                f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            )
            send_telegram_message(success_msg)
            return True
        else:
            send_telegram_message("❌ Bet placement failed!")
            return False

    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}")
        return False


def check_telegram_commands():
    """Check for new Telegram commands and execute them INSTANTLY"""
    updates = get_telegram_updates()

    for update in updates:
        try:
            if 'message' in update and 'text' in update['message']:
                message_text = update['message']['text']

                # Parse bet command
                bet_cmd = parse_bet_command(message_text)
                if bet_cmd:
                    print(f"⚡ INSTANT Telegram bet: {message_text}")

                    # Create managers
                    wallet_manager = WalletManager()
                    swap_manager = SwapManager()
                    betting_manager = BettingManager()

                    # Execute bet INSTANTLY
                    execute_telegram_bet(bet_cmd, wallet_manager, swap_manager, betting_manager)

        except Exception as e:
            print(f"⚠️ Error processing Telegram update: {e}")


class WalletManager:
    def load_wallets(self):
        try:
            if os.path.exists(self.wallets_file):
                with open(self.wallets_file, 'r') as f:
                    return json.load(f)
            return []
        except Exception as e:
            print(f"⚠️ Error loading wallets: {e}")
            return []

    def save_wallets(self):
        try:
            with open(self.wallets_file, 'w') as f:
                json.dump(self.wallets, f, indent=2)
        except Exception as e:
            print(f"⚠️ Error saving wallets: {e}")

    def __init__(self):
        self.wallets_file = "created_wallets.json"
        self.wallets = self.load_wallets()

    def create_new_wallet(self, name=None):
        try:
            private_key = "0x" + secrets.token_hex(32)
            account = Account.from_key(private_key)
            address = account.address
            if not name:
                name = f"Wallet_{len(self.wallets) + 1}_{datetime.now().strftime('%H%M%S')}"
            wallet_info = {
                "name": name,
                "address": address,
                "private_key": private_key,
                "created_at": datetime.now().isoformat(),
                "balance_bnb": 0,
                "balance_usdt": 0
            }
            self.wallets.append(wallet_info)
            self.save_wallets()
            print(f"✅ New wallet created!")
            print(f"📝 Name: {name}")
            print(f"📧 Address: {address}")
            print(f"🔑 Private Key: {private_key}")
            return wallet_info
        except Exception as e:
            print(f"❌ Error creating wallet: {e}")
            return None

    def get_wallet_balances(self, wallet_info):
        try:
            address = Web3.to_checksum_address(wallet_info["address"])
            bnb_balance = web3.eth.get_balance(address)
            bnb_balance = web3.from_wei(bnb_balance, 'ether')
            usdt_balance = usdt_contract.functions.balanceOf(address).call()
            usdt_balance = usdt_balance / 1e18
            wallet_info["balance_bnb"] = float(bnb_balance)
            wallet_info["balance_usdt"] = float(usdt_balance)
            return wallet_info
        except Exception as e:
            print(f"⚠️ Error getting balances: {e}")
            return wallet_info

    def list_wallets(self):
        if not self.wallets:
            print("📝 No wallets created yet.")
            return
        print("\n" + "=" * 80)
        print("📋 CREATED WALLETS")
        print("=" * 80)
        for i, wallet in enumerate(self.wallets):
            wallet = self.get_wallet_balances(wallet)
            print(f"{i + 1}. {wallet['name']}")
            print(f"   Address: {wallet['address']}")
            print(f"   BNB: {wallet['balance_bnb']:.6f}")
            print(f"   USDT: {wallet['balance_usdt']:.2f}")
            print(f"   Created: {wallet['created_at']}")
            print("-" * 80)

    def delete_wallet(self, wallet_index):
        try:
            if 0 <= wallet_index < len(self.wallets):
                deleted_wallet = self.wallets.pop(wallet_index)
                self.save_wallets()
                print(f"✅ Wallet '{deleted_wallet['name']}' deleted successfully!")
                return True
            else:
                print("❌ Invalid wallet index")
                return False
        except Exception as e:
            print(f"❌ Error deleting wallet: {e}")
            return False

    def empty_wallet(self, wallet_index, main_wallet_address):
        try:
            if not (0 <= wallet_index < len(self.wallets)):
                print("❌ Invalid wallet index")
                return False
            wallet = self.wallets[wallet_index]
            wallet = self.get_wallet_balances(wallet)
            if wallet['balance_bnb'] <= 0.00011:
                print(f"❌ Wallet '{wallet['name']}' has insufficient BNB to empty (need >0.001 BNB)")
                return False
            print(f"\n💸 Emptying wallet: {wallet['name']}")
            print(f"💰 Current balance: {wallet['balance_bnb']:.6f} BNB")
            print(f"📧 Sending to: {main_wallet_address}")
            gas_fee = web3.to_wei('0.0001', 'ether')
            total_balance = web3.eth.get_balance(Web3.to_checksum_address(wallet['address']))
            if total_balance <= gas_fee:
                print("❌ Balance too low to cover gas fees")
                return False
            amount_to_send = total_balance - gas_fee
            amount_bnb = web3.from_wei(amount_to_send, 'ether')
            print(f"📤 Sending amount: {amount_bnb:.6f} BNB")
            nonce = web3.eth.get_transaction_count(Web3.to_checksum_address(wallet['address']))
            tx = {
                'to': Web3.to_checksum_address(main_wallet_address),
                'value': amount_to_send,
                'gas': 21000,
                'gasPrice': web3.to_wei('0.1', 'gwei'),
                'nonce': nonce,
                'chainId': 56
            }
            signed_tx = web3.eth.account.sign_transaction(tx, wallet['private_key'])
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"🚀 Transaction sent! TX Hash: {web3.to_hex(tx_hash)}")
            print(f"⏳ Waiting for confirmation...")
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt.status == 1:
                print("✅ Wallet emptied successfully!")
                print(f"🔗 View on BSCScan: https://bscscan.com/tx/{web3.to_hex(tx_hash)}")
                message = (
                    f"💸 Wallet Emptied!\n\n"
                    f"👤 Wallet: {wallet['name']}\n"
                    f"💰 Amount: {amount_bnb:.6f} BNB\n"
                    f"📧 Sent to: Main Wallet\n"
                    f"🔗 TX: {web3.to_hex(tx_hash)}\n"
                    f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
                )
                send_telegram_message(message)
                return True
            else:
                print("❌ Transaction failed!")
                return False
        except Exception as e:
            print(f"❌ Error emptying wallet: {e}")
            return False


def drain_all_wallets(wallet_manager, main_wallet_address):
    any_drained = False
    for idx, wallet in enumerate(wallet_manager.wallets):
        wallet = wallet_manager.get_wallet_balances(wallet)
        if Web3.to_checksum_address(wallet['address']) == Web3.to_checksum_address(main_wallet_address):
            continue
        balance = wallet['balance_bnb']
        if balance <= 0.00001:
            print(f"🦴 Wallet {wallet['name']} has no dust to drain.")
            continue
        print(f"\n💀 Draining wallet {wallet['name']}... Current BNB: {balance:.8f}")
        try:
            address = Web3.to_checksum_address(wallet['address'])
            private_key = wallet['private_key']
            nonce = web3.eth.get_transaction_count(address)
            total_balance_wei = web3.eth.get_balance(address)
            gas_price = web3.to_wei('0.1', 'gwei')
            gas_limit = 21000
            gas_fee = gas_limit * gas_price
            if total_balance_wei <= gas_fee:
                print(f"❌ Not enough to cover gas in {wallet['name']}")
                continue
            value = total_balance_wei - gas_fee
            tx = {
                'to': Web3.to_checksum_address(main_wallet_address),
                'value': value,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
                'chainId': 56
            }
            signed_tx = web3.eth.account.sign_transaction(tx, private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"🚀 Draining... TX Hash: {web3.to_hex(tx_hash)}")
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt.status == 1:
                print(f"✅ Drained {wallet['name']}! Sent: {web3.from_wei(value, 'ether'):.8f} BNB")
                any_drained = True
            else:
                print(f"❌ Drain failed for {wallet['name']}")
            time.sleep(1)
        except Exception as e:
            print(f"❌ Error while draining {wallet['name']}: {e}")
    if any_drained:
        send_telegram_message("💀 All wallets drained! Dust sent to main wallet.")
    else:
        print("🦴 No wallets had dust to drain.")


def distribute_wealth(wallet_manager, main_wallet_address):
    """Distribute 95% of main wallet BNB equally to all sub-wallets"""
    try:
        if not wallet_manager.wallets:
            print("❌ No wallets available to distribute to.")
            return False

        # Get main wallet balance
        main_address = Web3.to_checksum_address(main_wallet_address)
        main_balance = web3.eth.get_balance(main_address)
        main_balance_bnb = web3.from_wei(main_balance, 'ether')

        if main_balance_bnb < 0.001:
            print(f"❌ Main wallet balance too low: {main_balance_bnb:.6f} BNB")
            return False

        # Calculate distribution amount (95% of balance)
        total_to_distribute = main_balance_bnb * Decimal('0.95')
        num_wallets = len(wallet_manager.wallets)
        amount_per_wallet = total_to_distribute / num_wallets

        # Reserve gas for transactions
        gas_per_tx = Decimal('0.00003')  # BNB for gas
        total_gas_needed = gas_per_tx * num_wallets

        if total_to_distribute < total_gas_needed:
            print(f"❌ Not enough balance to cover gas fees. Need at least {total_gas_needed:.6f} BNB")
            return False

        # Adjust per-wallet amount to account for gas
        amount_per_wallet = (total_to_distribute - total_gas_needed) / num_wallets

        print(f"\n💰 WEALTH DISTRIBUTION PREVIEW:")
        print(f"📊 Main wallet balance: {main_balance_bnb:.6f} BNB")
        print(f"💸 Total to distribute (95%): {total_to_distribute:.6f} BNB")
        print(f"👥 Number of wallets: {num_wallets}")
        print(f"🎯 Amount per wallet: {amount_per_wallet:.6f} BNB")
        print(f"⛽ Gas reserved: {total_gas_needed:.6f} BNB")

        confirm = input("\nProceed with distribution? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ Distribution cancelled.")
            return False

        print(f"\n🚀 Starting distribution to {num_wallets} wallets...")

        successful_transfers = 0
        failed_transfers = 0

        for i, wallet in enumerate(wallet_manager.wallets):
            try:
                wallet_address = Web3.to_checksum_address(wallet['address'])
                wallet_name = wallet['name']

                print(f"\n📤 Sending to wallet {i + 1}/{num_wallets}: {wallet_name}")
                print(f"   Address: {wallet_address}")
                print(f"   Amount: {amount_per_wallet:.6f} BNB")

                # Get current nonce
                nonce = web3.eth.get_transaction_count(main_address)

                # Build transaction
                tx = {
                    'to': wallet_address,
                    'value': web3.to_wei(amount_per_wallet, 'ether'),
                    'gas': 21000,
                    'gasPrice': web3.to_wei('0.1', 'gwei'),
                    'nonce': nonce,
                    'chainId': 56
                }

                # Sign and send transaction
                signed_tx = web3.eth.account.sign_transaction(tx, MAIN_PRIVATE_KEY)
                tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)

                print(f"   🚀 TX Hash: {web3.to_hex(tx_hash)}")

                # Wait for confirmation
                receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

                if receipt.status == 1:
                    print(f"   ✅ Success!")
                    successful_transfers += 1
                else:
                    print(f"   ❌ Failed!")
                    failed_transfers += 1

                # Small delay between transactions
                time.sleep(1)

            except Exception as e:
                print(f"   ❌ Error sending to {wallet_name}: {e}")
                failed_transfers += 1
                continue

        print(f"\n🎉 DISTRIBUTION COMPLETE!")
        print(f"✅ Successful transfers: {successful_transfers}")
        print(f"❌ Failed transfers: {failed_transfers}")
        print(f"💰 Total distributed: {successful_transfers * amount_per_wallet:.6f} BNB")

        # Send Telegram notification
        if successful_transfers > 0:
            message = (
                f"💰 Wealth Distribution Complete!\n\n"
                f"✅ Successful: {successful_transfers}/{num_wallets} wallets\n"
                f"💸 Per wallet: {amount_per_wallet:.6f} BNB\n"
                f"💎 Total distributed: {successful_transfers * amount_per_wallet:.6f} BNB\n"
                f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            )
            send_telegram_message(message)

        return successful_transfers > 0

    except Exception as e:
        print(f"❌ Error during wealth distribution: {e}")
        return False

class SwapManager:
    def __init__(self):
        pass

    def get_usdt_to_bnb_rate(self, usdt_amount):
        try:
            usdt_amount_wei = int(usdt_amount * 1e18)
            path = [USDT_CONTRACT, WBNB]
            amounts = router_contract.functions.getAmountsOut(
                usdt_amount_wei, path
            ).call()
            bnb_amount = amounts[1] / 1e18
            return bnb_amount
        except Exception as e:
            print(f"⚠️ Error getting swap rate: {e}")
            return 0

    def swap_usdt_to_bnb(self, usdt_amount, recipient_address):
        try:
            print(f"\n🔄 Starting USDT to BNB swap...")
            print(f"💰 Amount: {usdt_amount} USDT")
            print(f"📧 Recipient: {recipient_address}")

            main_address = Web3.to_checksum_address(MAIN_WALLET_ADDRESS)
            usdt_balance = usdt_contract.functions.balanceOf(main_address).call() / 1e18

            if usdt_balance < usdt_amount:
                print(f"❌ Insufficient USDT balance. Have: {usdt_balance:.2f}, Need: {usdt_amount}")
                return False

            expected_bnb = self.get_usdt_to_bnb_rate(usdt_amount)
            print(f"📊 Expected BNB: {expected_bnb:.6f}")

            allowance = usdt_contract.functions.allowance(
                main_address, PANCAKE_ROUTER
            ).call()
            usdt_amount_wei = int(usdt_amount * 1e18)

            if allowance < usdt_amount_wei:
                print("🔓 Approving USDT spending...")
                nonce = web3.eth.get_transaction_count(main_address)
                approve_tx = usdt_contract.functions.approve(
                    PANCAKE_ROUTER, usdt_amount_wei * 2
                ).build_transaction({
                    'from': main_address,
                    'gas': 100000,
                    'gasPrice': web3.to_wei('0.1', 'gwei'),
                    'nonce': nonce
                })
                signed_tx = web3.eth.account.sign_transaction(approve_tx, MAIN_PRIVATE_KEY)
                tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
                print(f"⏳ Waiting for approval... TX: {web3.to_hex(tx_hash)}")
                web3.eth.wait_for_transaction_receipt(tx_hash)
                print("✅ Approval confirmed!")

            print("🔄 Executing swap...")
            deadline = int(time.time()) + 300
            min_bnb_out = int(expected_bnb * 0.999 * 1e18)
            nonce = web3.eth.get_transaction_count(main_address)
            swap_tx = router_contract.functions.swapExactTokensForETH(
                usdt_amount_wei,
                min_bnb_out,
                [USDT_CONTRACT, WBNB],
                recipient_address,
                deadline
            ).build_transaction({
                'from': main_address,
                'gas': 300000,
                'gasPrice': web3.to_wei('0.1', 'gwei'),
                'nonce': nonce
            })
            signed_tx = web3.eth.account.sign_transaction(swap_tx, MAIN_PRIVATE_KEY)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"⏳ Waiting for swap... TX: {web3.to_hex(tx_hash)}")
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt.status == 1:
                print("✅ Swap completed successfully!")
                print(f"🔗 TX Hash: {web3.to_hex(tx_hash)}")
                return True
            else:
                print("❌ Swap failed!")
                return False
        except Exception as e:
            print(f"❌ Error during swap: {e}")
            return False


def swap_usdt_to_bnb_main_wallet(usdt_amount):
    main_address = Web3.to_checksum_address(MAIN_WALLET_ADDRESS)
    usdt_balance = usdt_contract.functions.balanceOf(main_address).call() / 1e18
    if usdt_balance < usdt_amount:
        print(f"❌ Insufficient USDT balance. You have {usdt_balance:.4f} USDT.")
        return
    path = [USDT_CONTRACT, WBNB]
    usdt_amount_wei = int(usdt_amount * 1e18)
    amounts = router_contract.functions.getAmountsOut(usdt_amount_wei, path).call()
    expected_bnb = amounts[1] / 1e18
    print(f"\n💱 You will swap {usdt_amount} USDT → {expected_bnb:.6f} BNB (approx.)")
    allowance = usdt_contract.functions.allowance(main_address, PANCAKE_ROUTER).call()
    if allowance < usdt_amount_wei:
        print("🔓 Approving USDT for PancakeSwap...")
        nonce = web3.eth.get_transaction_count(main_address)
        approve_tx = usdt_contract.functions.approve(
            PANCAKE_ROUTER, usdt_amount_wei * 2
        ).build_transaction({
            'from': main_address,
            'gas': 100000,
            'gasPrice': web3.to_wei('0.1', 'gwei'),
            'nonce': nonce
        })
        signed_approve = web3.eth.account.sign_transaction(approve_tx, MAIN_PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed_approve.raw_transaction)
        print(f"⏳ Waiting for approval... TX: {web3.to_hex(tx_hash)}")
        web3.eth.wait_for_transaction_receipt(tx_hash)
        print("✅ Approval confirmed.")
    confirm = input(f"Proceed with swap? (y/n): ").strip().lower()
    if confirm != 'y':
        print("❌ Swap cancelled.")
        return
    min_bnb_out = int(expected_bnb * 0.999 * 1e18)
    deadline = int(time.time()) + 300
    nonce = web3.eth.get_transaction_count(main_address)
    swap_tx = router_contract.functions.swapExactTokensForETH(
        usdt_amount_wei,
        min_bnb_out,
        [USDT_CONTRACT, WBNB],
        main_address,
        deadline
    ).build_transaction({
        'from': main_address,
        'gas': 300000,
        'gasPrice': web3.to_wei('0.1', 'gwei'),
        'nonce': nonce
    })
    signed_swap = web3.eth.account.sign_transaction(swap_tx, MAIN_PRIVATE_KEY)
    tx_hash = web3.eth.send_raw_transaction(signed_swap.raw_transaction)
    print(f"⏳ Waiting for swap TX confirmation... TX: {web3.to_hex(tx_hash)}")
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status == 1:
        print(f"✅ Swap completed! TX: https://bscscan.com/tx/{web3.to_hex(tx_hash)}")
    else:
        print("❌ Swap failed.")


def swap_bnb_to_usdt_main_wallet(bnb_amount):
    """Swap BNB to USDT from main wallet (0.1% slippage)"""
    try:
        main_address = Web3.to_checksum_address(MAIN_WALLET_ADDRESS)
        bnb_balance = web3.eth.get_balance(main_address) / 1e18
        if bnb_balance < bnb_amount:
            print(f"❌ Insufficient BNB balance. You have {bnb_balance:.4f} BNB.")
            return

        path = [WBNB, USDT_CONTRACT]
        bnb_amount_wei = int(bnb_amount * 1e18)
        amounts = router_contract.functions.getAmountsOut(bnb_amount_wei, path).call()
        expected_usdt = amounts[1] / 1e18

        print(f"\n💱 You will swap {bnb_amount} BNB → {expected_usdt:.4f} USDT (approx.)")

        confirm = input(f"Proceed with swap? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ Swap cancelled.")
            return

        min_usdt_out = int(expected_usdt * 0.999 * 1e18)
        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(main_address)

        swap_tx = router_contract.functions.swapExactETHForTokens(
            min_usdt_out,
            path,
            main_address,
            deadline
        ).build_transaction({
            'from': main_address,
            'value': bnb_amount_wei,
            'gas': 300000,
            'gasPrice': web3.to_wei('0.1', 'gwei'),
            'nonce': nonce
        })

        signed_swap = web3.eth.account.sign_transaction(swap_tx, MAIN_PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed_swap.raw_transaction)
        print(f"⏳ Waiting for swap TX confirmation... TX: {web3.to_hex(tx_hash)}")
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status == 1:
            print(f"✅ Swap completed! TX: https://bscscan.com/tx/{web3.to_hex(tx_hash)}")
        else:
            print("❌ Swap failed.")

    except Exception as e:
        print(f"❌ Error during main wallet BNB→USDT swap: {e}")


class BettingManager:
    def __init__(self):
        pass

    def place_bet(self, wallet_info, direction, bet_amount_bnb):
        """Place a bet using the specified wallet"""
        try:
            current_epoch = prediction_contract.functions.currentEpoch().call()
            round_data = prediction_contract.functions.rounds(current_epoch).call()
            current_time = int(time.time())
            lock_timestamp = round_data[2]
            if current_time >= lock_timestamp:
                print("⚠️ Current round is locked, cannot place bets")
                return False

            print(f"\n🎯 Placing bet...")
            print(f"👤 Wallet: {wallet_info['name']}")
            print(f"📊 Direction: {direction.upper()}")
            print(f"💰 Amount: {bet_amount_bnb} BNB")
            print(f"🔢 Round: {current_epoch}")
            print(f"⏰ Time remaining: {lock_timestamp - current_time} seconds")

            address = Web3.to_checksum_address(wallet_info['address'])
            private_key = wallet_info['private_key']
            balance = web3.eth.get_balance(address)
            balance_bnb = web3.from_wei(balance, 'ether')
            bet_amount_wei = web3.to_wei(bet_amount_bnb, 'ether')

            if balance < bet_amount_wei + web3.to_wei('0.00003', 'ether'):
                print(f"❌ Insufficient balance. Have: {balance_bnb:.6f} BNB")
                return False

            if direction.lower() == 'up':
                function = prediction_contract.functions.betBull(current_epoch)
            else:
                function = prediction_contract.functions.betBear(current_epoch)

            nonce = web3.eth.get_transaction_count(address)

            tx = function.build_transaction({
                'from': address,
                'value': bet_amount_wei,
                'gas': 200000,
                'gasPrice': web3.to_wei('0.1', 'gwei'),
                'nonce': nonce
            })

            signed_tx = web3.eth.account.sign_transaction(tx, private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)

            print(f"🚀 Bet placed! TX Hash: {web3.to_hex(tx_hash)}")
            print(f"🔗 View on BSCScan: https://bscscan.com/tx/{web3.to_hex(tx_hash)}")
            return True

        except Exception as e:
            print(f"❌ Error placing bet: {e}")
            return False


class RewardManager:
    def __init__(self):
        pass

    def get_claimable_epochs(self, wallet_address):
        """Get all epochs where wallet has claimable rewards"""
        try:
            claimable_epochs = []
            wallet_address = Web3.to_checksum_address(wallet_address)

            # Get current epoch to know the range to check
            current_epoch = prediction_contract.functions.currentEpoch().call()

            # Check last 100 rounds (you can adjust this range)
            start_epoch = max(1, current_epoch - 5)

            print(f"🔍 Checking epochs {start_epoch} to {current_epoch - 1} for claimable rewards...")

            for epoch in range(start_epoch, current_epoch):
                try:
                    # Check if user has bet in this round
                    user_round = prediction_contract.functions.ledger(epoch, wallet_address).call()

                    # user_round structure: [position, amount, claimed]
                    # position: 0 = Bull, 1 = Bear
                    # amount: bet amount
                    # claimed: True if already claimed

                    if user_round[1] > 0 and not user_round[2]:  # Has bet and not claimed
                        # Check if round is claimable (finished and user won)
                        if prediction_contract.functions.claimable(epoch, wallet_address).call():
                            round_data = prediction_contract.functions.rounds(epoch).call()
                            claimable_epochs.append({
                                'epoch': epoch,
                                'bet_amount': web3.from_wei(user_round[1], 'ether'),
                                'position': 'BULL' if user_round[0] == 0 else 'BEAR',
                                'claimed': user_round[2]
                            })

                except Exception as e:
                    # Skip epochs that cause errors (might not exist yet)
                    continue

            return claimable_epochs

        except Exception as e:
            print(f"❌ Error getting claimable epochs: {e}")
            return []

    def get_claimable_amount(self, wallet_address, epoch):
        """Get the claimable amount for a specific epoch"""
        try:
            wallet_address = Web3.to_checksum_address(wallet_address)

            # This calls the contract's view function to calculate rewards
            # Note: This might not exist in all prediction contracts
            # Alternative: calculate based on round data

            user_round = prediction_contract.functions.ledger(epoch, wallet_address).call()
            round_data = prediction_contract.functions.rounds(epoch).call()

            if user_round[1] > 0 and not user_round[2]:  # Has bet and not claimed
                bet_amount = user_round[1]

                # Get total amounts for calculation
                # round_data structure varies, but typically includes:
                # [startTimestamp, lockTimestamp, closeTimestamp, lockPrice, closePrice, lockOracleId, closeOracleId, totalAmount, bullAmount, bearAmount, rewardBaseCalAmount, rewardAmount, oraclesCalled]

                total_amount = round_data[7]  # totalAmount
                bull_amount = round_data[8]  # bullAmount
                bear_amount = round_data[9]  # bearAmount
                reward_amount = round_data[11]  # rewardAmount

                # Calculate user's share of the rewards
                if user_round[0] == 0:  # Bull position
                    if bull_amount > 0:
                        user_reward = (bet_amount * reward_amount) // bull_amount
                    else:
                        user_reward = 0
                else:  # Bear position
                    if bear_amount > 0:
                        user_reward = (bet_amount * reward_amount) // bear_amount
                    else:
                        user_reward = 0

                return web3.from_wei(user_reward, 'ether')

            return 0

        except Exception as e:
            print(f"⚠️ Error calculating claimable amount: {e}")
            return 0

    def claim_rewards(self, wallet_info, epochs_to_claim=None):
        """Claim rewards for specified epochs or all claimable epochs"""
        try:
            wallet_address = Web3.to_checksum_address(wallet_info['address'])
            private_key = wallet_info['private_key']

            # Get all claimable epochs if none specified
            if epochs_to_claim is None:
                claimable_epochs = self.get_claimable_epochs(wallet_address)
                epochs_to_claim = [epoch['epoch'] for epoch in claimable_epochs]

            if not epochs_to_claim:
                print("🎉 No rewards to claim!")
                return True

            print(f"\n🎁 Claiming rewards for {len(epochs_to_claim)} epochs...")

            successful_claims = 0
            total_claimed = 0

            for epoch in epochs_to_claim:
                try:
                    print(f"🎯 Claiming epoch {epoch}...")

                    # Check if still claimable
                    if not prediction_contract.functions.claimable(epoch, wallet_address).call():
                        print(f"⚠️ Epoch {epoch} is not claimable, skipping...")
                        continue

                    # Get estimated reward amount
                    estimated_reward = self.get_claimable_amount(wallet_address, epoch)

                    # Build claim transaction
                    nonce = web3.eth.get_transaction_count(wallet_address)

                    claim_tx = prediction_contract.functions.claim([epoch]).build_transaction({
                        'from': wallet_address,
                        'gas': 200000,
                        'gasPrice': web3.to_wei('0.1', 'gwei'),
                        'nonce': nonce,
                        'chainId': 56
                    })

                    # Sign and send transaction
                    signed_tx = web3.eth.account.sign_transaction(claim_tx, private_key)
                    tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)

                    print(f"⏳ Waiting for claim confirmation... TX: {web3.to_hex(tx_hash)}")
                    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

                    if receipt.status == 1:
                        print(f"✅ Claimed epoch {epoch}! Estimated reward: {estimated_reward:.6f} BNB")
                        print(f"🔗 TX: https://bscscan.com/tx/{web3.to_hex(tx_hash)}")
                        successful_claims += 1
                        total_claimed += estimated_reward
                    else:
                        print(f"❌ Failed to claim epoch {epoch}")

                    # Small delay between claims
                    time.sleep(2)

                except Exception as e:
                    print(f"❌ Error claiming epoch {epoch}: {e}")
                    continue

            print(f"\n🎉 CLAIM SUMMARY:")
            print(f"✅ Successfully claimed: {successful_claims}/{len(epochs_to_claim)} epochs")
            print(f"💰 Total estimated rewards: {total_claimed:.6f} BNB")

            if successful_claims > 0:
                # Send Telegram notification
                message = (
                    f"🎁 Rewards Claimed!\n\n"
                    f"👤 Wallet: {wallet_info['name']}\n"
                    f"✅ Epochs claimed: {successful_claims}\n"
                    f"💰 Total rewards: {total_claimed:.6f} BNB\n"
                    f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
                )
                send_telegram_message(message)

            return successful_claims > 0

        except Exception as e:
            print(f"❌ Error during reward claiming: {e}")
            return False

    def show_claimable_rewards(self, wallet_info):
        """Show all claimable rewards for a wallet"""
        try:
            print(f"\n🔍 Checking claimable rewards for: {wallet_info['name']}")
            print(f"📧 Address: {wallet_info['address']}")

            claimable_epochs = self.get_claimable_epochs(wallet_info['address'])

            if not claimable_epochs:
                print("🎉 No claimable rewards found!")
                return

            print(f"\n💎 CLAIMABLE REWARDS ({len(claimable_epochs)} epochs):")
            print("=" * 80)

            total_claimable = 0
            for epoch_data in claimable_epochs:
                estimated_reward = self.get_claimable_amount(wallet_info['address'], epoch_data['epoch'])
                total_claimable += estimated_reward

                print(f"🎯 Epoch {epoch_data['epoch']}")
                print(f"   Position: {epoch_data['position']}")
                print(f"   Bet Amount: {epoch_data['bet_amount']:.6f} BNB")
                print(f"   Estimated Reward: {estimated_reward:.6f} BNB")
                print("-" * 80)

            print(f"💰 TOTAL ESTIMATED REWARDS: {total_claimable:.6f} BNB")

            return claimable_epochs

        except Exception as e:
            print(f"❌ Error showing claimable rewards: {e}")
            return []


def send_telegram_message(message):
    try:
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        response = requests.post(url, data=payload)
        if not response.ok:
            print(f"⚠️ Telegram error: {response.text}")
    except Exception as e:
        print(f"⚠️ Telegram exception: {e}")


def main():
    wallet_manager = WalletManager()
    swap_manager = SwapManager()
    betting_manager = BettingManager()
    reward_manager = RewardManager()

    print("🤖 Multi-Wallet Prediction Bot")
    print("⚡ INSTANT TELEGRAM BETTING ACTIVE!")
    print("📱 Send: /bet 1/50/up")
    print("=" * 50)

    def telegram_monitor():
        """INSTANT Telegram monitoring - NO DELAYS ⚡"""
        while True:
            try:
                check_telegram_commands()
                # NO SLEEP = INSTANT EXECUTION ⚡⚡⚡
            except Exception as e:
                print(f"⚠️ Telegram monitor error: {e}")
                time.sleep(1)  # Only sleep on errors

    # Start INSTANT Telegram monitoring
    telegram_thread = threading.Thread(target=telegram_monitor, daemon=True)
    telegram_thread.start()
    print("⚡ INSTANT Telegram monitor started!")

    while True:
        print("\n📋 MAIN MENU:")
        print("1. Check main wallet balance")
        print("2. Swap BNB to USDT (Main Wallet, 0.1% slippage)")
        print("3. Swap USDT to BNB (Main Wallet, 0.1% slippage)")
        print("4. List all wallets")
        print("5. Create new wallet")
        print("6. Start betting process")
        print("7. Claim rewards")
        print("8. Empty wallet (send all BNB to main wallet)")
        print("9. Drain all wallets (send ALL BNB to main wallet)")
        print("10. Distribute wealth (send 95% of main wallet equally to all wallets)")
        print("11. Delete wallet")
        print("12. Show total BNB balance of all sub-wallets (exclude main wallet)")
        print("13. Exit")
        print("\n⚡ INSTANT TELEGRAM: /bet [wallet]/[usdt]/[up|down]")

        choice = input("\nSelect option (1-12): ").strip()

        if choice == '1':
            try:
                main_address = Web3.to_checksum_address(MAIN_WALLET_ADDRESS)
                bnb_balance = web3.eth.get_balance(main_address)
                bnb_balance = web3.from_wei(bnb_balance, 'ether')
                usdt_balance = usdt_contract.functions.balanceOf(main_address).call()
                usdt_balance = usdt_balance / 1e18
                print(f"\n💰 MAIN WALLET BALANCE:")
                print(f"📧 Address: {MAIN_WALLET_ADDRESS}")
                print(f"💎 BNB: {bnb_balance:.6f}")
                print(f"💵 USDT: {usdt_balance:.2f}")
            except Exception as e:
                print(f"❌ Error checking balance: {e}")
        elif choice == '2':
            try:
                bnb_balance = web3.eth.get_balance(Web3.to_checksum_address(MAIN_WALLET_ADDRESS)) / 1e18
                print(f"\n💎 Main Wallet BNB Balance: {bnb_balance:.4f} BNB")
                amount = float(input("Enter BNB amount to swap: "))
                if amount <= 0 or amount > bnb_balance:
                    print("❌ Invalid amount.")
                else:
                    swap_bnb_to_usdt_main_wallet(amount)
            except Exception as e:
                print(f"❌ Error: {e}")
        elif choice == '3':
            try:
                usdt_balance = usdt_contract.functions.balanceOf(
                    Web3.to_checksum_address(MAIN_WALLET_ADDRESS)).call() / 1e18
                print(f"\n💵 Main Wallet USDT Balance: {usdt_balance:.4f} USDT")
                amount = float(input("Enter USDT amount to swap: "))
                if amount <= 0 or amount > usdt_balance:
                    print("❌ Invalid amount.")
                else:
                    swap_usdt_to_bnb_main_wallet(amount)
            except Exception as e:
                print(f"❌ Error: {e}")
        elif choice == '4':
            wallet_manager.list_wallets()
        elif choice == '5':
            name = input("Enter wallet name (or press Enter for auto-name): ").strip()
            if not name:
                name = None
            wallet_manager.create_new_wallet(name)
        elif choice == '6':
            wallet_manager.list_wallets()
            if not wallet_manager.wallets:
                print("❌ No wallets available. Create a wallet first.")
                continue
            try:
                wallet_idx = int(input("\nSelect wallet number: ")) - 1
                if wallet_idx < 0 or wallet_idx >= len(wallet_manager.wallets):
                    print("❌ Invalid wallet selection")
                    continue
                selected_wallet = wallet_manager.wallets[wallet_idx]
            except ValueError:
                print("❌ Invalid input")
                continue
            try:
                usdt_amount = float(input("Enter USDT amount to convert and send: "))
                if usdt_amount <= 0:
                    print("❌ Amount must be positive")
                    continue
            except ValueError:
                print("❌ Invalid amount")
                continue
            direction = input("Enter bet direction (up/down): ").strip().lower()
            if direction not in ['up', 'down']:
                print("❌ Direction must be 'up' or 'down'")
                continue
            expected_bnb = swap_manager.get_usdt_to_bnb_rate(usdt_amount)
            print(f"\n📊 TRANSACTION PREVIEW:")
            print(f"💱 {usdt_amount} USDT → ~{expected_bnb:.6f} BNB")
            print(f"📧 Recipient: {selected_wallet['name']}")
            print(f"🎯 Bet Direction: {direction.upper()}")
            confirm = input("\nConfirm transaction? (y/n): ").strip().lower()
            if confirm != 'y':
                print("❌ Transaction cancelled")
                continue
            success = swap_manager.swap_usdt_to_bnb(
                usdt_amount,
                selected_wallet['address']
            )
            if success:
                print("✅ Swap completed! Waiting 0.5 seconds before placing bet...")
                time.sleep(0.5)
                selected_wallet = wallet_manager.get_wallet_balances(selected_wallet)
                bet_amount = selected_wallet['balance_bnb'] * 0.95
                betting_success = betting_manager.place_bet(
                    selected_wallet,
                    direction,
                    bet_amount
                )
                if betting_success:
                    message = (
                        f"🤖 Multi-Wallet Bot Activity\n\n"
                        f"💱 Swapped: {usdt_amount} USDT → {expected_bnb:.6f} BNB\n"
                        f"🎯 Bet: {direction.upper()} with {bet_amount:.6f} BNB\n"
                        f"👤 Wallet: {selected_wallet['name']}\n"
                        f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    send_telegram_message(message)
                    print("🎉 Complete process finished successfully!")
                else:
                    print("❌ Bet placement failed")
            else:
                print("❌ Swap failed")
        elif choice == '7':
            wallet_manager.list_wallets()
            if not wallet_manager.wallets:
                print("❌ No wallets available.")
                continue
            try:
                wallet_idx = int(input("\nSelect wallet number: ")) - 1
                if wallet_idx < 0 or wallet_idx >= len(wallet_manager.wallets):
                    print("❌ Invalid wallet selection")
                    continue
                selected_wallet = wallet_manager.wallets[wallet_idx]
            except ValueError:
                print("❌ Invalid input")
                continue
            claimable_epochs = []
            if hasattr(reward_manager, "show_claimable_rewards"):
                claimable_epochs = reward_manager.show_claimable_rewards(selected_wallet)
            if claimable_epochs:
                print("\n🎯 CLAIM OPTIONS:")
                print("1. Claim all rewards")
                print("2. Show details only")
                print("3. Cancel")
                claim_choice = input("\nSelect option (1-3): ").strip()
                if claim_choice == '1':
                    confirm = input("\n🎁 Confirm claiming all rewards? (y/n): ").strip().lower()
                    if confirm == 'y' and hasattr(reward_manager, "claim_rewards"):
                        success = reward_manager.claim_rewards(selected_wallet)
                        if success:
                            print("🎉 Rewards claimed successfully!")
                        else:
                            print("❌ Failed to claim rewards")
                    else:
                        print("❌ Claim cancelled")
                elif claim_choice == '2':
                    print("✅ Details shown above")
                else:
                    print("❌ Cancelled")
        elif choice == '8':
            wallet_manager.list_wallets()
            if not wallet_manager.wallets:
                print("❌ No wallets available to empty.")
                continue
            try:
                wallet_idx = int(input("\nSelect wallet number to empty: ")) - 1
                if wallet_idx < 0 or wallet_idx >= len(wallet_manager.wallets):
                    print("❌ Invalid wallet selection")
                    continue
                selected_wallet = wallet_manager.wallets[wallet_idx]
                selected_wallet = wallet_manager.get_wallet_balances(selected_wallet)
                print(f"\n💸 EMPTY WALLET PREVIEW:")
                print(f"👤 Wallet: {selected_wallet['name']}")
                print(f"💰 Current BNB: {selected_wallet['balance_bnb']:.6f}")
                print(f"📧 Will send to: {MAIN_WALLET_ADDRESS}")
                print("⚠️ All BNB will be sent back to your main wallet")
                confirm = input("\nConfirm emptying wallet? (y/n): ").strip().lower()
                if confirm == 'y':
                    wallet_manager.empty_wallet(wallet_idx, MAIN_WALLET_ADDRESS)
                else:
                    print("❌ Operation cancelled")
            except ValueError:
                print("❌ Invalid input")
        elif choice == '9':
            confirm = input(
                "⚠️ This will send ALL BNB from ALL wallets to your main wallet.\nProceed? (y/n): ").strip().lower()
            if confirm == 'y':
                drain_all_wallets(wallet_manager, MAIN_WALLET_ADDRESS)
            else:
                print("❌ Operation cancelled")
        elif choice == '10':
            wallet_manager.list_wallets()
            if not wallet_manager.wallets:
                print("❌ No wallets available to distribute to.")
                continue

            main_address = Web3.to_checksum_address(MAIN_WALLET_ADDRESS)
            main_balance_bnb = web3.from_wei(web3.eth.get_balance(main_address), 'ether')

            print(f"\n💰 Current main wallet balance: {main_balance_bnb:.6f} BNB")
            print(f"💸 Will distribute: {main_balance_bnb * Decimal('0.95'):.6f} BNB (95%)")
            print(f"👥 To {len(wallet_manager.wallets)} wallets")
            print(f"🎯 Each wallet gets: {(main_balance_bnb * Decimal('0.95')) / len(wallet_manager.wallets):.6f} BNB")

            confirm = input("\n💰 Confirm wealth distribution? (y/n): ").strip().lower()
            if confirm == 'y':
                distribute_wealth(wallet_manager, MAIN_WALLET_ADDRESS)
            else:
                print("❌ Distribution cancelled")
        elif choice == '11':
            wallet_manager.list_wallets()
            if not wallet_manager.wallets:
                print("❌ No wallets available to delete.")
                continue
            try:
                wallet_idx = int(input("\nSelect wallet number to delete: ")) - 1
                if wallet_idx < 0 or wallet_idx >= len(wallet_manager.wallets):
                    print("❌ Invalid wallet selection")
                    continue
                selected_wallet = wallet_manager.wallets[wallet_idx]
                print(f"\n⚠️ WARNING: You are about to delete wallet '{selected_wallet['name']}'")
                print(f"📧 Address: {selected_wallet['address']}")
                print("🔥 This action cannot be undone!")
                confirm = input("\nType 'DELETE' to confirm: ").strip()
                if confirm == 'DELETE':
                    wallet_manager.delete_wallet(wallet_idx)
                else:
                    print("❌ Deletion cancelled")
            except ValueError:
                print("❌ Invalid input")
        elif choice == '12':
            total_bnb = 0
            for wallet in wallet_manager.wallets:
                # Skip main wallet
                if Web3.to_checksum_address(wallet['address']) == Web3.to_checksum_address(MAIN_WALLET_ADDRESS):
                    continue
                wallet = wallet_manager.get_wallet_balances(wallet)
                total_bnb += wallet['balance_bnb']
            print(f"\n💰 TOTAL BNB BALANCE (All sub-wallets, excluding main wallet): {total_bnb:.6f} BNB")
        elif choice == '13':
            print("👋 Goodbye!")
            break
        elif choice == '13':
            print("👋 Goodbye!")
            break
        else:
            print("❌ Invalid option")


if __name__ == "__main__":
    main()
