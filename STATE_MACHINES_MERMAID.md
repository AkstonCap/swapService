# State Machine Diagrams (Mermaid Format)

These diagrams can be rendered in GitHub, VS Code (with Mermaid extension), or any Mermaid-compatible viewer.

## 1. USDC→USDD Complete Flow

```mermaid
stateDiagram-v2
    [*] --> SolanaDeposit: USDC deposit detected

    state "Solana Deposit Detection" as SolanaDeposit {
        [*] --> DuplicateCheck
        DuplicateCheck --> Skip: Already exists
        DuplicateCheck --> AddUnprocessed: New deposit
        AddUnprocessed --> ReadyForProcessing
    }

    state "Signature Processing" as SigProcessing {
        ReadyForProcessing --> ValidateMemo
        
        state ValidateMemo <<choice>>
        ValidateMemo --> ValidMemo: nexus:address format
        ValidateMemo --> ToBeRefunded1: Invalid memo
        
        ValidMemo --> CheckAccount
        
        state CheckAccount <<choice>>
        CheckAccount --> ValidAccount: is_valid_usdd_account
        CheckAccount --> ToBeRefunded2: Invalid Nexus account
        
        ValidAccount --> CalculateFees
        CalculateFees --> MicroDeposit: Net amount ≤ 0
        CalculateFees --> AttemptDebit: Net amount > 0
        
        MicroDeposit --> Processed_FeeOnly
        
        AttemptDebit --> DebitResult
        
        state DebitResult <<choice>>
        DebitResult --> DebitedAwaitingConfirm: Success
        DebitResult --> ToBeRefunded3: Failed
    }

    state "Refund Flow" as RefundFlow {
        ToBeRefunded1 --> ValidateFromAddr
        ToBeRefunded2 --> ValidateFromAddr
        ToBeRefunded3 --> ValidateFromAddr
        
        state ValidateFromAddr <<choice>>
        ValidateFromAddr --> SendRefund: Valid token account
        ValidateFromAddr --> ToBeQuarantined: Invalid address
        
        SendRefund --> RefundSentAwaiting
        RefundSentAwaiting --> CheckRefundConfirm
        
        state CheckRefundConfirm <<choice>>
        CheckRefundConfirm --> Refunded: Confirmed
        CheckRefundConfirm --> RefundSentAwaiting: Pending
    }

    state "Quarantine Flow" as QuarantineFlow {
        ToBeQuarantined --> SendQuarantine
        SendQuarantine --> QuarantineResult
        
        state QuarantineResult <<choice>>
        QuarantineResult --> Quarantined: Success
        QuarantineResult --> QuarantineFailed: Failed
    }

    state "Confirmation Check" as ConfirmCheck {
        DebitedAwaitingConfirm --> CheckNexusConfirm
        
        state CheckNexusConfirm <<choice>>
        CheckNexusConfirm --> Processed: Confirmed
        CheckNexusConfirm --> DebitedAwaitingConfirm: Pending
    }

    Skip --> [*]
    Processed_FeeOnly --> [*]
    Processed --> [*]
    Refunded --> [*]
    Quarantined --> [*]
    QuarantineFailed --> QuarantineFailed: STUCK! No retry
```

## 2. USDD→USDC Complete Flow

```mermaid
stateDiagram-v2
    [*] --> NexusCredit: USDD credit detected

    state "Nexus Credit Detection" as NexusCredit {
        [*] --> ThresholdCheck
        
        state ThresholdCheck <<choice>>
        ThresholdCheck --> Ignore: Below MIN_CREDIT_USDD
        ThresholdCheck --> DupCheck: Above threshold
        
        DupCheck --> Skip: Already exists
        DupCheck --> CheckFeeOnly: New credit
        
        state CheckFeeOnly <<choice>>
        CheckFeeOnly --> ProcessedAsFees: Amount ≤ fees
        CheckFeeOnly --> AddUnprocessedTxid: Amount > fees
        
        AddUnprocessedTxid --> PendingReceival
    }

    state "Receival Resolution" as ReceivalResolution {
        PendingReceival --> WaitConfirmations: confirmations ≤ 1
        WaitConfirmations --> PendingReceival
        PendingReceival --> LookupAsset: confirmations > 1
        
        LookupAsset --> AssetResult
        
        state AssetResult <<choice>>
        AssetResult --> ValidReceival: Found & valid USDC account
        AssetResult --> InvalidReceival: Found but invalid
        AssetResult --> NotFound: No asset yet
        
        ValidReceival --> ReadyForProcessing
        InvalidReceival --> AttemptRefund1
        NotFound --> CheckTimeout
        
        state CheckTimeout <<choice>>
        CheckTimeout --> AttemptRefund2: Timeout exceeded
        CheckTimeout --> PendingReceival: Wait more
    }

    state "USDC Send Flow" as USDCSend {
        ReadyForProcessing --> Sending
        Sending --> SendUSdc
        
        SendUSdc --> SendResult
        
        state SendResult <<choice>>
        SendResult --> SigAwaitingConfirm: Success
        SendResult --> RecoverSig: Failed (check memo)
        
        RecoverSig --> RecoveryResult
        
        state RecoveryResult <<choice>>
        RecoveryResult --> SigAwaitingConfirm: Found sig
        RecoveryResult --> RefundPending: Not found
    }

    state "Refund Handling" as RefundHandling {
        AttemptRefund1 --> RefundResult1
        AttemptRefund2 --> RefundResult2
        
        state RefundResult1 <<choice>>
        RefundResult1 --> RefundedTxid: Success
        RefundResult1 --> CheckAttempts1: Failed
        
        state RefundResult2 <<choice>>
        RefundResult2 --> RefundedTxid: Success
        RefundResult2 --> CheckAttempts2: Failed
        
        state CheckAttempts1 <<choice>>
        CheckAttempts1 --> QuarantinedTxid: Max attempts
        CheckAttempts1 --> RefundPending: Retry later
        
        state CheckAttempts2 <<choice>>
        CheckAttempts2 --> QuarantinedTxid: Max attempts
        CheckAttempts2 --> TradeBalanceCheck: Retry later
    }

    state "Confirmation Flow" as ConfirmFlow {
        SigAwaitingConfirm --> CheckSolanaConfirm
        
        state CheckSolanaConfirm <<choice>>
        CheckSolanaConfirm --> ProcessedTxid: Confirmed
        CheckSolanaConfirm --> SigAwaitingConfirm: Pending
    }

    Ignore --> [*]
    Skip --> [*]
    ProcessedAsFees --> [*]
    RefundedTxid --> [*]
    ProcessedTxid --> [*]
    QuarantinedTxid --> [*]
    TradeBalanceCheck --> TradeBalanceCheck: STUCK! No handler
    RefundPending --> RefundPending: STUCK! Needs retry logic
```

## 3. Main Event Loop

```mermaid
stateDiagram-v2
    [*] --> Startup

    state "Startup Phase" as Startup {
        [*] --> PrintInfo
        PrintInfo --> FetchBalances
        FetchBalances --> StartupRecovery
        StartupRecovery --> BalanceReconcile
        BalanceReconcile --> SetupSignals
        SetupSignals --> [*]
    }

    Startup --> MainLoop

    state "Main Loop" as MainLoop {
        [*] --> CheckStop
        
        state CheckStop <<choice>>
        CheckStop --> Maintenance: Not stopped
        CheckStop --> [*]: Stop requested
        
        state "Maintenance" as Maintenance {
            [*] --> BackingCheck
            BackingCheck --> PeriodicReconcile
            PeriodicReconcile --> BalanceCheck10m
            BalanceCheck10m --> FeeConversions
            FeeConversions --> Metrics
            Metrics --> [*]
        }
        
        Maintenance --> ShouldPause
        
        state ShouldPause <<choice>>
        ShouldPause --> WaitInterval: Pause required
        ShouldPause --> Polling: Continue
        
        WaitInterval --> CheckStop
        
        state "Polling Phase" as Polling {
            [*] --> SolanaPoll
            SolanaPoll --> NexusPoll
            NexusPoll --> NexusProcess
            NexusProcess --> [*]
        }
        
        Polling --> CheckStop
    }

    MainLoop --> Shutdown

    state "Shutdown" as Shutdown {
        [*] --> Cleanup
        Cleanup --> [*]
    }

    Shutdown --> [*]
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
        T1["debited, awaiting confirmation<br/>No timeout → stuck forever"]
        T2["sig created, awaiting confirmations<br/>No timeout → stuck forever"]
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

    %% USDC → USDD Flow
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

    %% USDD → USDC Flow
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

    %% Heartbeat
    SOLANA_POLL -.->|Update| NXS_HEART
    NEXUS_POLL -.->|Update| NXS_HEART
```
