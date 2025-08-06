# USDC ↔ USDD Bidirectional Swap Service

A Python service that enables automatic swapping between USDC (Solana) and USDD (Nexus) tokens in both directions.

## 🔄 How It Works

### USDC → USDD (Solana to Nexus)
1. User sends USDC to your Solana vault with memo: `nexus:<NEXUS_ADDRESS>`
2. Service validates the Nexus address exists
3. Service sends equivalent USDD to the Nexus address

### USDD → USDC (Nexus to Solana)
1. User sends USDD to your Nexus account with reference: `solana:<SOLANA_ADDRESS>`
2. Service validates the Solana address format
3. Service sends equivalent USDC from vault to the Solana address

## 📋 Prerequisites

- Python 3.8+
- A Solana wallet with USDC vault setup
- A Nexus node running locally
- USDD tokens in your Nexus account for outgoing swaps

## 🛠️ Setup Instructions

### 1. Install Dependencies

```bash
pip install python-dotenv solana anchorpy spl-token
```

### 2. Solana Wallet Setup

#### Create or Import Vault Wallet
```bash
# Generate new keypair (or use existing)
solana-keygen new --outfile vault-keypair.json

# Get your wallet address
solana address --keypair vault-keypair.json
```

#### Create USDC Token Account
```bash
# Create USDC token account for your vault
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v --keypair vault-keypair.json

# Note down the token account address - this is your VAULT_USDC_ACCOUNT
```

#### Fund Your Vault
```bash
# Transfer some SOL for transaction fees
solana transfer <VAULT_ADDRESS> 0.1 --keypair your-main-keypair.json

# Transfer USDC for swaps (optional - will be received from users)
spl-token transfer EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v <AMOUNT> <VAULT_USDC_ACCOUNT> --keypair your-main-keypair.json
```

### 3. Nexus Node Setup

#### Install and Run Nexus Node
```bash
# Download Nexus from https://github.com/Nexusoft/LLL-TAO
# Build and start the node locally
./nexus
```

#### Create USDD Account for receival
```bash
# Create or use existing USDD account
./nexus finance/create/account token=USDD name=USDD_account pin=<YOUR_PIN>

# Note down the account address - this is your NEXUS_USDD_ACCOUNT
```

#### Fund USDD Account (if you're not the owner of the token)
Ensure your USDD account has sufficient USDD tokens for outgoing swaps.

### 4. Environment Configuration

Create a `.env` file in the project directory:

```env
# Solana Configuration
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
# For devnet: https://api.devnet.solana.com
# For local: http://127.0.0.1:8899

# Vault Settings
VAULT_KEYPAIR=./vault-keypair.json
VAULT_USDC_ACCOUNT=<YOUR_VAULT_USDC_TOKEN_ACCOUNT_ADDRESS>
USDC_MINT=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
# For devnet USDC: 4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU

# Nexus Configuration
NEXUS_CLI_PATH=./nexus
NEXUS_PIN=<YOUR_NEXUS_PIN>
NEXUS_USDD_ACCOUNT=<YOUR_USDD_ACCOUNT_ADDRESS>
NEXUS_TOKEN_NAME=USDD

# Optional Settings
POLL_INTERVAL=10
PROCESSED_SIG_FILE=processed_sigs.json
PROCESSED_NEXUS_FILE=processed_nexus_txs.json
```

### 5. Security Setup

#### Secure Your Private Keys
```bash
# Set restrictive permissions on keypair file
chmod 600 vault-keypair.json

# Consider using a hardware wallet for production
```

#### Environment Variables Security
```bash
# Set restrictive permissions on .env file
chmod 600 .env

# Never commit .env to version control
echo ".env" >> .gitignore
```

## 🚀 Running the Service

### Start the Swap Service
```bash
python swapService.py
```

### Expected Output
```
🌐 Starting bidirectional swap service
   Solana RPC: https://api.mainnet-beta.solana.com
   USDC Vault: <YOUR_VAULT_ADDRESS>
   USDD Account: <YOUR_USDD_ACCOUNT>
   Monitoring:
   - USDC → USDD: Solana deposits with Nexus address in memo
   - USDD → USDC: USDD deposits with Solana address in reference
```

## 📖 User Instructions

### For Users Swapping USDC → USDD

1. **Send USDC to the vault address** with memo format:
   ```
   nexus:<YOUR_NEXUS_ADDRESS>
   ```

2. **Example using Solana CLI:**
   ```bash
   spl-token transfer EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 100 <VAULT_USDC_ACCOUNT> --memo "nexus:YOUR_NEXUS_ADDRESS"
   ```

3. **Wait for confirmation** - USDD will be sent to your Nexus address

### For Users Swapping USDD → USDC

1. **Send USDD to the service's USDD account** with reference:
   ```bash
   ./nexus finance/debit/account from=<YOUR_USDD_ACCOUNT> to=<SERVICE_USDD_ACCOUNT> amount=100 reference="solana:<YOUR_SOLANA_ADDRESS>" pin=<YOUR_PIN>
   ```

2. **Wait for confirmation** - USDC will be sent to your Solana address

## 🔧 Configuration Options

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `SOLANA_RPC_URL` | Solana RPC endpoint | ✅ | - |
| `VAULT_KEYPAIR` | Path to vault keypair JSON | ✅ | - |
| `VAULT_USDC_ACCOUNT` | Vault's USDC token account | ✅ | - |
| `USDC_MINT` | USDC mint address | ✅ | - |
| `NEXUS_PIN` | Nexus account PIN | ✅ | - |
| `NEXUS_USDD_ACCOUNT` | Your USDD account address | ✅ | - |
| `NEXUS_CLI_PATH` | Path to Nexus CLI | ❌ | `./nexus` |
| `NEXUS_TOKEN_NAME` | Token name in Nexus | ❌ | `USDD` |
| `POLL_INTERVAL` | Polling interval in seconds | ❌ | `10` |

## 🔍 Monitoring & Logs

### Service Logs
The service outputs detailed logs for all operations:
- ✅ Successful swaps
- ❌ Failed operations
- 🔍 Address validations
- 📝 Transaction processing

### State Files
- `processed_sigs.json` - Tracks processed Solana transactions
- `processed_nexus_txs.json` - Tracks processed Nexus transactions

## ⚠️ Important Notes

### Security Considerations
- **Keep your vault keypair secure** - it controls USDC funds
- **Monitor your USDD balance** - ensure sufficient funds for swaps
- **Use hardware wallets** for production environments
- **Regular backups** of keypairs and state files

### Network Considerations
- **Transaction fees** - Ensure SOL balance for Solana transactions
- **Confirmation times** - Wait for network confirmations
- **Rate limiting** - Be mindful of RPC rate limits

### Error Handling
- Failed transactions are not marked as processed
- Service retries failed operations on next poll
- Check logs for detailed error information

## 🆘 Troubleshooting

### Common Issues

#### "Required environment variable not set"
- Check your `.env` file exists and has all required variables
- Verify file permissions allow reading

#### "Error validating Nexus address"
- Ensure Nexus node is running and accessible
- Check if the provided Nexus address exists

#### "Error sending USDC/USDD"
- Verify sufficient balances in vault/USDD account
- Check network connectivity
- Ensure proper permissions and PINs

#### "Timeout errors"
- Check network connectivity
- Verify RPC endpoints are responsive
- Consider increasing timeout values

### Getting Help

1. **Check the logs** for detailed error messages
2. **Verify network status** (Solana/Nexus)
3. **Test with small amounts** first
4. **Check account balances** before swapping

## 📝 License

This project is provided as-is for educational and development purposes. Use at your own risk in production environments.

## 🤝 Contributing

Feel free to submit issues and enhancement requests!
