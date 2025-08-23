# Enhanced DoS Protection Implementation

## Updated Thresholds (0.100101 for both sides)

### **USDC Deposits (Enhanced)**
- **MIN_DEPOSIT_USDC = "0.100101"** (100,101 micro-units)
- **Effect**: Deposits below 0.100101 USDC become 100% fees
- **Protection**: Prevents micro-deposit vault drainage

### **USDD Credits (New Protection)**  
- **MIN_CREDIT_USDD = "0.100101"** (100,101 micro-units)
- **Effect**: Credits below 0.100101 USDD stay in treasury as fees
- **Protection**: Prevents micro-credit processing DoS

## Processing Limits

### **Batch Processing Controls**
```python
MAX_DEPOSITS_PER_LOOP = 100    # USDC side limit
MAX_CREDITS_PER_LOOP = 100     # USDD side limit
```

### **Fee Penalties**
```python
MICRO_DEPOSIT_FEE_PCT = 100    # 100% fee for USDC spam
MICRO_CREDIT_FEE_PCT = 100     # 100% fee for USDD spam
```

## Attack Scenarios Now Blocked

### **Scenario 1: USDC Micro-Deposit Spam**
```
Before: 1M × 0.000001 USDC → vault fee drainage
After:  1M × 0.000001 USDC → 100% fees to service
Result: Attack becomes unprofitable, generates revenue
```

### **Scenario 2: USDD Micro-Credit Spam**
```
Before: 1M × 0.000001 USDD → service crash (memory/CPU)
After:  1M × 0.000001 USDD → treasury keeps all, minimal processing
Result: Attack neutralized, treasury benefits
```

### **Scenario 3: Mixed Attack**
```
Before: USDC + USDD spam → complete service failure
After:  Both sides protected → attack becomes donation to service
Result: Dual-sided protection ensures resilience
```

## Economic Impact

**Legitimate Users**: 
- Unaffected (normal swaps > 0.100101)
- Slightly higher threshold than round 0.1 prevents edge cases

**Attackers**: 
- USDC spam → lose money to service fees
- USDD spam → donate to treasury with no service impact  
- Mixed attacks → double loss, zero impact

**Service**:
- Gains revenue from spam attempts
- Maintains performance under attack
- No resource exhaustion or crashes

## Technical Benefits

1. **Memory Protection**: Limited processing prevents OOM
2. **Storage Protection**: Fewer entries in state files  
3. **RPC Protection**: Reduced Nexus/Solana API load
4. **Waterline Protection**: Micro-transactions don't block progression
5. **Economic Deterrent**: Attacks become self-defeating

The 0.100101 threshold creates an effective economic moat while preserving legitimate swap functionality.
