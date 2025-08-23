"""Balance reconciliation for USDC→USDD direction to prevent double-minting.

Since Nexus debit references are limited to uint64 integers (can't include USDC deposit signatures),
we verify that the cumulative USDD balance for each recipient address matches expected deposits minus fees.

Logic:
1. For each USDD address that received credits from treasury, calculate expected balance:
   Expected = Sum(USDC deposits with valid memo) - Sum(fees) converted to USDD units
2. Query actual Nexus balance for that address
3. If actual > expected, flag potential double-mint and optionally issue corrective refund

This catches scenarios where a USDC deposit was processed twice due to state loss.
"""
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Tuple
from . import config, state, nexus_client, solana_client
import json
import os


def _read_jsonl_safe(path: str) -> List[Dict]:
    """Read JSONL file safely, returning empty list on error."""
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _usdc_to_usdd_units(usdc_units: int) -> int:
    """Convert USDC base units to USDD base units using configured decimals."""
    if config.USDC_DECIMALS == config.USDD_DECIMALS:
        return usdc_units
    if config.USDC_DECIMALS < config.USDD_DECIMALS:
        return usdc_units * (10 ** (config.USDD_DECIMALS - config.USDC_DECIMALS))
    return usdc_units // (10 ** (config.USDC_DECIMALS - config.USDD_DECIMALS))


def calculate_expected_balances() -> Dict[str, Dict]:
    """Calculate expected USDD balances for each address based on processed USDC deposits.
    
    Returns:
        Dict[usdd_address, {
            'expected_usdd_units': int,
            'deposits': List[{sig, amount_usdc, net_usdd, fees}],
            'total_deposits_usdc': int,
            'total_fees_usdc': int
        }]
    """
    expected = {}
    
    # Scan processed swaps (successful USDC→USDD)
    processed_swaps = _read_jsonl_safe(config.PROCESSED_SWAPS_FILE)
    
    for row in processed_swaps:
        try:
            # Skip non-USDC-deposit entries
            if not row.get('sig') or row.get('comment') in ('refunded', 'quarantined', 'processed, smaller than fees'):
                continue
                
            # Must have receival_account (USDD address) and processing succeeded
            usdd_addr = row.get('receival_account')
            if not usdd_addr or row.get('comment') != 'processed':
                continue
                
            amount_usdc = int(row.get('amount_usdc_units', 0))
            if amount_usdc <= 0:
                continue
                
            # Calculate fees and net USDD
            flat_fee = max(0, int(getattr(config, 'FLAT_FEE_USDC_UNITS', 0)))
            dynamic_bps = max(0, int(getattr(config, 'DYNAMIC_FEE_BPS', 0)))
            pre_dynamic = max(0, amount_usdc - flat_fee)
            dynamic_fee = (pre_dynamic * dynamic_bps) // 10000
            total_fee = flat_fee + dynamic_fee
            net_usdc = max(0, amount_usdc - total_fee)
            net_usdd = _usdc_to_usdd_units(net_usdc)
            
            if usdd_addr not in expected:
                expected[usdd_addr] = {
                    'expected_usdd_units': 0,
                    'deposits': [],
                    'total_deposits_usdc': 0,
                    'total_fees_usdc': 0
                }
                
            expected[usdd_addr]['expected_usdd_units'] += net_usdd
            expected[usdd_addr]['total_deposits_usdc'] += amount_usdc
            expected[usdd_addr]['total_fees_usdc'] += total_fee
            expected[usdd_addr]['deposits'].append({
                'sig': row.get('sig'),
                'amount_usdc': amount_usdc,
                'net_usdd': net_usdd,
                'fees': total_fee,
                'ts': row.get('ts', 0)
            })
            
        except Exception:
            continue
            
    return expected


def get_actual_usdd_balance(usdd_address: str) -> int:
    """Get actual USDD balance for an address from Nexus."""
    try:
        account_info = nexus_client.get_account_info(usdd_address)
        if not account_info:
            return 0
            
        # Check if this is a valid USDD token account
        if not nexus_client.is_expected_token(account_info, config.NEXUS_TOKEN_NAME):
            return 0
            
        # Extract balance
        balance = account_info.get('balance')
        if balance is None and isinstance(account_info.get('result'), dict):
            balance = account_info['result'].get('balance')
            
        if balance is not None:
            return int(Decimal(str(balance)))
        return 0
        
    except Exception:
        return 0


def reconcile_balances(dry_run: bool = True) -> Dict:
    """Reconcile expected vs actual USDD balances.
    
    Args:
        dry_run: If True, only report discrepancies. If False, issue corrective refunds.
        
    Returns:
        {
            'checked_addresses': int,
            'discrepancies': List[{
                'address': str,
                'expected': int,
                'actual': int,
                'surplus': int,
                'action_taken': str
            }],
            'total_surplus_usdd': int
        }
    """
    expected_balances = calculate_expected_balances()
    discrepancies = []
    total_surplus = 0
    
    for usdd_addr, expected_data in expected_balances.items():
        try:
            expected_usdd = expected_data['expected_usdd_units']
            actual_usdd = get_actual_usdd_balance(usdd_addr)
            
            # Allow small tolerance for rounding differences
            tolerance = max(1, expected_usdd // 1000000)  # 0.0001% tolerance
            surplus = actual_usdd - expected_usdd
            
            if surplus > tolerance:
                action_taken = "dry_run" if dry_run else "none"
                
                # Issue corrective refund if not dry run
                if not dry_run and surplus > 0:
                    try:
                        # Refund surplus USDD back to treasury
                        success = nexus_client.transfer_usdd_between_accounts(
                            from_addr=usdd_addr,
                            to_addr=config.NEXUS_USDD_TREASURY_ACCOUNT,
                            amount_usdd_units=surplus,
                            reference=f"balance_correction_{usdd_addr[:8]}"
                        )
                        action_taken = "refunded" if success else "refund_failed"
                    except Exception:
                        action_taken = "refund_error"
                
                discrepancies.append({
                    'address': usdd_addr,
                    'expected': expected_usdd,
                    'actual': actual_usdd,
                    'surplus': surplus,
                    'deposits_count': len(expected_data['deposits']),
                    'total_deposits_usdc': expected_data['total_deposits_usdc'],
                    'action_taken': action_taken
                })
                total_surplus += surplus
                
        except Exception as e:
            discrepancies.append({
                'address': usdd_addr,
                'expected': expected_data['expected_usdd_units'],
                'actual': 0,
                'surplus': 0,
                'error': str(e),
                'action_taken': 'error'
            })
    
    return {
        'checked_addresses': len(expected_balances),
        'discrepancies': discrepancies,
        'total_surplus_usdd': total_surplus,
        'dry_run': dry_run
    }


def log_balance_discrepancies(result: Dict):
    """Log balance reconciliation results."""
    if not result['discrepancies']:
        print(f"[balance_check] ✓ All {result['checked_addresses']} USDD addresses match expected balances")
        return
        
    print(f"[balance_check] ⚠ Found {len(result['discrepancies'])} discrepancies out of {result['checked_addresses']} addresses:")
    print(f"[balance_check] Total surplus: {result['total_surplus_usdd']} USDD units")
    
    for disc in result['discrepancies'][:5]:  # Show first 5
        addr_short = disc['address'][:8] + "..." if len(disc['address']) > 12 else disc['address']
        print(f"[balance_check]   {addr_short}: expected={disc['expected']} actual={disc['actual']} surplus={disc['surplus']} action={disc['action_taken']}")
        
    if len(result['discrepancies']) > 5:
        print(f"[balance_check]   ... and {len(result['discrepancies']) - 5} more")


def run_balance_reconciliation(dry_run: bool = True, enable_corrections: bool = False) -> Dict:
    """Main entry point for balance reconciliation.
    
    Args:
        dry_run: If True, only report discrepancies
        enable_corrections: If True and dry_run=False, issue corrective refunds
        
    Returns:
        Reconciliation results dict
    """
    if not dry_run and not enable_corrections:
        print("[balance_check] Corrective actions disabled; running as dry_run")
        dry_run = True
        
    try:
        result = reconcile_balances(dry_run=dry_run)
        log_balance_discrepancies(result)
        return result
    except Exception as e:
        print(f"[balance_check] Error during reconciliation: {e}")
        return {
            'checked_addresses': 0,
            'discrepancies': [],
            'total_surplus_usdd': 0,
            'error': str(e)
        }
