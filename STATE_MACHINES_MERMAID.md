# State Machine Diagrams (Mermaid Format)

These diagrams can be rendered in GitHub, VS Code (with Mermaid extension), or any Mermaid-compatible viewer.

## 1. USDC→USDD Complete Flow

```mermaid
flowchart TD
    START((Start)) --> DEPOSIT[USDC Deposit Detected]
    
    subgraph Detection["Solana Deposit Detection"]
        DEPOSIT --> DUP_CHECK{Already exists?}
        DUP_CHECK -->|Yes| SKIP[Skip]
        DUP_CHECK -->|No| ADD_UNPROC[Add to unprocessed_sigs]
        ADD_UNPROC --> READY[ready for processing]
    end
    
    subgraph Processing["Signature Processing"]
        READY --> VALIDATE_MEMO{Memo valid?<br/>nexus:address}
        VALIDATE_MEMO -->|No| TO_REFUND1[to be refunded]
        VALIDATE_MEMO -->|Yes| CHECK_ACCT{Valid USDD account?}
        CHECK_ACCT -->|No| TO_REFUND2[to be refunded]
        CHECK_ACCT -->|Yes| CALC_FEES[Calculate fees]
        CALC_FEES --> NET_CHECK{Net amount > 0?}
        NET_CHECK -->|No| FEE_ONLY[Processed as fee-only]
        NET_CHECK -->|Yes| DEBIT[Attempt USDD debit]
        DEBIT --> DEBIT_OK{Debit success?}
        DEBIT_OK -->|Yes| AWAITING[debited, awaiting confirmation]
        DEBIT_OK -->|No| TO_REFUND3[to be refunded]
    end
    
    subgraph Refund["Refund Flow"]
        TO_REFUND1 --> VALID_FROM{Valid from address?}
        TO_REFUND2 --> VALID_FROM
        TO_REFUND3 --> VALID_FROM
        VALID_FROM -->|No| TO_QUAR[to be quarantined]
        VALID_FROM -->|Yes| SEND_REF[Send refund]
        SEND_REF --> REF_AWAIT[refund sent, awaiting confirmation]
        REF_AWAIT --> REF_CONF{Confirmed?}
        REF_CONF -->|Yes| REFUNDED[Refunded ✓]
        REF_CONF -->|No - pending| REF_AWAIT
    end
    
    subgraph Quarantine["Quarantine Flow"]
        TO_QUAR --> SEND_QUAR[Send to quarantine]
        SEND_QUAR --> QUAR_OK{Success?}
        QUAR_OK -->|Yes| QUARANTINED[Quarantined ✓]
        QUAR_OK -->|No| QUAR_FAIL[quarantine failed ⚠️]
    end
    
    subgraph Confirmation["Debit Confirmation"]
        AWAITING --> CHECK_CONF{Nexus confirmed?}
        CHECK_CONF -->|Yes| PROCESSED[Processed ✓]
        CHECK_CONF -->|No - pending| AWAITING
    end
    
    SKIP --> END_STATE((End))
    FEE_ONLY --> END_STATE
    PROCESSED --> END_STATE
    REFUNDED --> END_STATE
    QUARANTINED --> END_STATE
    QUAR_FAIL -.->|STUCK - No retry| QUAR_FAIL
```

## 2. USDD→USDC Complete Flow

```mermaid
flowchart TD
    START((Start)) --> CREDIT[USDD Credit Detected]
    
    subgraph Detection["Nexus Credit Detection"]
        CREDIT --> THRESH{Above MIN_CREDIT?}
        THRESH -->|No| IGNORE[Ignored]
        THRESH -->|Yes| DUP{Already exists?}
        DUP -->|Yes| SKIP[Skip]
        DUP -->|No| FEE_CHK{Amount > fees?}
        FEE_CHK -->|No| FEES[processed as fees]
        FEE_CHK -->|Yes| ADD_TXID[Add to unprocessed_txids]
        ADD_TXID --> PENDING[pending_receival]
    end
    
    subgraph Resolution["Receival Resolution"]
        PENDING --> CONF_CHK{confirmations > 1?}
        CONF_CHK -->|No| PENDING
        CONF_CHK -->|Yes| LOOKUP[Lookup asset by txid+owner]
        LOOKUP --> ASSET_RESULT{Asset found?}
        ASSET_RESULT -->|Valid USDC account| READY_PROC[ready for processing]
        ASSET_RESULT -->|Invalid account| REF_INVALID[Attempt refund]
        ASSET_RESULT -->|Not found| TIMEOUT{Timeout exceeded?}
        TIMEOUT -->|No| PENDING
        TIMEOUT -->|Yes| REF_TIMEOUT[Attempt refund]
    end
    
    subgraph Sending["USDC Send Flow"]
        READY_PROC --> SENDING[sending]
        SENDING --> SEND_USDC[Send USDC]
        SEND_USDC --> SEND_OK{Success?}
        SEND_OK -->|Yes| SIG_AWAIT[sig created, awaiting confirmations]
        SEND_OK -->|No| RECOVER[Check for existing memo]
        RECOVER --> FOUND{Sig found?}
        FOUND -->|Yes| SIG_AWAIT
        FOUND -->|No| MAX_ATT{Max attempts?}
        MAX_ATT -->|No| READY_PROC
        MAX_ATT -->|Yes| REF_PEND[refund pending]
    end
    
    subgraph RefundFlow["Refund Handling"]
        REF_INVALID --> REF_RESULT1{Refund success?}
        REF_TIMEOUT --> REF_RESULT2{Refund success?}
        REF_RESULT1 -->|Yes| REFUNDED[Refunded ✓]
        REF_RESULT1 -->|No| ATTEMPTS1{Max attempts?}
        ATTEMPTS1 -->|Yes| QUAR[Quarantined]
        ATTEMPTS1 -->|No| REF_PEND
        REF_RESULT2 -->|Yes| REFUNDED
        REF_RESULT2 -->|No| ATTEMPTS2{Max attempts?}
        ATTEMPTS2 -->|Yes| QUAR
        ATTEMPTS2 -->|No| TRADE_BAL[trade balance to be checked ⚠️]
    end
    
    subgraph Confirm["USDC Confirmation"]
        SIG_AWAIT --> SOL_CONF{Solana confirmed?}
        SOL_CONF -->|Yes| PROCESSED[Processed ✓]
        SOL_CONF -->|No| SIG_AWAIT
    end
    
    IGNORE --> END_STATE((End))
    SKIP --> END_STATE
    FEES --> END_STATE
    REFUNDED --> END_STATE
    PROCESSED --> END_STATE
    QUAR --> END_STATE
    TRADE_BAL -.->|STUCK - No handler| TRADE_BAL
    REF_PEND -.->|STUCK - Needs retry| REF_PEND
```

## 3. Main Event Loop

```mermaid
flowchart TD
    START((Start)) --> STARTUP
    
    subgraph STARTUP["Startup Phase"]
        S1[Print info] --> S2[Fetch balances]
        S2 --> S3[Startup recovery]
        S3 --> S4[Balance reconcile]
        S4 --> S5[Setup signals]
    end
    
    S5 --> LOOP
    
    subgraph LOOP["Main Loop"]
        STOP_CHK{Stop requested?}
        STOP_CHK -->|Yes| SHUTDOWN
        STOP_CHK -->|No| MAINT
        
        subgraph MAINT["Maintenance"]
            M1[Backing check] --> M2[Periodic reconcile]
            M2 --> M3[Balance check 10min]
            M3 --> M4[Fee conversions]
            M4 --> M5[Metrics]
        end
        
        MAINT --> PAUSE{Should pause?}
        PAUSE -->|Yes| WAIT[Wait interval]
        WAIT --> STOP_CHK
        PAUSE -->|No| POLL
        
        subgraph POLL["Polling Phase"]
            P1[poll_solana_deposits] --> P2[poll_nexus_usdd_deposits]
            P2 --> P3[process_unprocessed_txids]
        end
        
        POLL --> STOP_CHK
    end
    
    subgraph SHUTDOWN["Shutdown"]
        SD1[Cleanup]
    end
    
    SHUTDOWN --> END_STATE((End))
```

## 4. Fee Calculation Flow

```mermaid
flowchart TD
    subgraph USDC_to_USDD["USDC → USDD Fees"]
        A1[Deposit Amount USDC] --> B1{Amount >= MIN_DEPOSIT?}
        B1 -->|No| C1[100% Fee - Ignored]
        B1 -->|Yes| D1[Subtract FLAT_FEE_USDC_UNITS]
        D1 --> E1[Calculate DYNAMIC_FEE_BPS]
        E1 --> F1[Net USDD = Amount - Flat - Dynamic]
        F1 --> G1{Net > 0?}
        G1 -->|No| H1[Mark as fee-only]
        G1 -->|Yes| I1[Debit USDD to user]
    end

    subgraph USDD_to_USDC["USDD → USDC Fees"]
        A2[Credit Amount USDD] --> B2{Amount >= MIN_CREDIT?}
        B2 -->|No| C2[Ignored entirely]
        B2 -->|Yes| D2[Subtract FLAT_FEE_USDD]
        D2 --> E2[Calculate DYNAMIC_FEE_BPS]
        E2 --> F2[Net USDC = Amount - Flat - Dynamic]
        F2 --> G2{Net > 0?}
        G2 -->|No| H2[Mark as fees]
        G2 -->|Yes| I2[Send USDC to user]
    end

    subgraph Reconciliation["Backing Reconciliation"]
        R1[vault_usdc] --> R2[circ_usdd]
        R2 --> R3{vault > circ?}
        R3 -->|No| R4[Check PAUSE threshold]
        R3 -->|Yes| R5[surplus = vault - circ]
        R5 --> R6{surplus >= threshold?}
        R6 -->|Yes| R7[Mint USDD to fees account]
        R6 -->|No| R8[No action]
        R4 --> R9{vault < 90% circ?}
        R9 -->|Yes| R10[PAUSE SERVICE]
        R9 -->|No| R11[Continue]
    end
```

## 5. Waterline Management

```mermaid
flowchart TD
    subgraph Solana_Waterline["Solana Waterline"]
        SW1[poll_solana_deposits] --> SW2[Get oldest unprocessed timestamp]
        SW2 --> SW3[proposed_wl = min_ts - SAFETY_SEC]
        SW3 --> SW4[propose_solana_waterline]
        SW4 --> SW5[waterline_proposals table]
    end

    subgraph Nexus_Waterline["Nexus Waterline"]
        NW1[poll_nexus_usdd_deposits] --> NW2[Get oldest unprocessed timestamp]
        NW2 --> NW3[proposed_wl = min_ts - SAFETY_SEC]
        NW3 --> NW4[propose_nexus_waterline]
        NW4 --> NW5[waterline_proposals table]
    end

    subgraph Heartbeat["Heartbeat Update"]
        H1[update_heartbeat_asset] --> H2[Write to Nexus blockchain]
        H2 --> H3[last_safe_timestamp_solana]
        H2 --> H4[last_safe_timestamp_usdd]
    end

    SW5 --> H1
    NW5 --> H1

    subgraph Missing["⚠️ MISSING IMPLEMENTATION"]
        M1[apply_waterline_proposals] --> M2[Read proposals]
        M2 --> M3[Apply to heartbeat]
        M3 --> M4[Clear proposals table]
    end

    style Missing fill:#ffcccc
```

## 6. Gap Analysis - Stuck States

```mermaid
flowchart TD
    subgraph Stuck_States["States With No Exit Path"]
        S1["quarantine failed"]
        S2["trade balance to be checked"]
        S3["collecting refund"]
        S4["memo unresolved<br/>(never set)"]
    end

    subgraph Missing_Timeouts["Missing Timeout Handlers"]
        T1["debited, awaiting confirmation<br/>No timeout - stuck forever"]
        T2["sig created, awaiting confirmations<br/>No timeout - stuck forever"]
        T3["ready for processing<br/>STALE_DEPOSIT_QUARANTINE_SEC<br/>defined but not used"]
    end

    subgraph Missing_Handlers["Missing Event Handlers"]
        H1["Owner mismatch<br/>Logged but no action"]
        H2["Waterline proposals<br/>Written but not applied"]
        H3["process_unprocessed_entries<br/>Commented out in main.py"]
    end

    style Stuck_States fill:#ff6666
    style Missing_Timeouts fill:#ffcc66
    style Missing_Handlers fill:#ffff66
```

## 7. Complete System Flow

```mermaid
flowchart TB
    subgraph Solana["Solana Network"]
        SOL_USER[User Wallet]
        SOL_VAULT[Vault USDC Account]
        SOL_QUAR[Quarantine Account]
    end

    subgraph Nexus["Nexus Network"]
        NXS_USER[User USDD Account]
        NXS_TREAS[Treasury USDD]
        NXS_FEES[Fees USDD Account]
        NXS_ASSET[distordiaSwap Asset]
        NXS_HEART[Heartbeat Asset]
    end

    subgraph SwapService["Swap Service"]
        subgraph DB["SQLite Database"]
            UNPROC_SIG[unprocessed_sigs]
            PROC_SIG[processed_sigs]
            REF_SIG[refunded_sigs]
            QUAR_SIG[quarantined_sigs]
            UNPROC_TXID[unprocessed_txids]
            PROC_TXID[processed_txids]
            REF_TXID[refunded_txids]
            QUAR_TXID[quarantined_txids]
        end

        SOLANA_POLL[swap_solana.py<br/>poll_solana_deposits]
        NEXUS_POLL[swap_nexus.py<br/>poll_nexus_usdd_deposits]
        NEXUS_PROC[swap_nexus.py<br/>process_unprocessed_txids]
    end

    SOL_USER -->|1. Send USDC + memo| SOL_VAULT
    SOL_VAULT -->|2. Detect deposit| SOLANA_POLL
    SOLANA_POLL -->|3. Queue| UNPROC_SIG
    UNPROC_SIG -->|4. Process| SOLANA_POLL
    SOLANA_POLL -->|5. Debit USDD| NXS_USER
    SOLANA_POLL -->|6. Mark done| PROC_SIG
    SOLANA_POLL -->|6a. Refund| SOL_USER
    SOLANA_POLL -->|6a. Mark| REF_SIG
    SOLANA_POLL -->|6b. Quarantine| SOL_QUAR
    SOLANA_POLL -->|6b. Mark| QUAR_SIG

    NXS_USER -->|1. Send USDD| NXS_TREAS
    NXS_USER -->|2. Publish asset| NXS_ASSET
    NXS_TREAS -->|3. Detect credit| NEXUS_POLL
    NEXUS_POLL -->|4. Queue| UNPROC_TXID
    UNPROC_TXID -->|5. Process| NEXUS_PROC
    NXS_ASSET -->|6. Lookup receival| NEXUS_PROC
    NEXUS_PROC -->|7. Send USDC| SOL_USER
    NEXUS_PROC -->|8. Mark done| PROC_TXID
    NEXUS_PROC -->|8a. Refund| NXS_USER
    NEXUS_PROC -->|8a. Mark| REF_TXID
    NEXUS_PROC -->|8b. Quarantine| QUAR_TXID

    SOLANA_POLL -.->|Update| NXS_HEART
    NEXUS_POLL -.->|Update| NXS_HEART
```
