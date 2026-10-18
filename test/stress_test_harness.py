import time
import json
import asyncio
import threading
from typing import List, Dict, Any
from fastapi.testclient import TestClient
from main import app
from services.websocket_manager import connection_manager

def run_concurrent_connections_test(num_clients: int = 50) -> Dict[str, Any]:
    """
    Stress-tests concurrent WebSocket connections to /ws/orders/ord_stress_1.
    Measures setup time, message broadcast delivery across all connections, and cleanup.
    """
    print(f"\n--- Running Concurrent Connections Test ({num_clients} clients) ---")
    order_id = "ord_stress_1"
    token = "test-valid-token"
    clients = []
    websockets = []
    start_time = time.time()
    
    # 1. Establish concurrent connections
    connected_count = 0
    connection_errors = 0
    
    for i in range(num_clients):
        try:
            tc = TestClient(app)
            ws = tc.websocket_connect(f"/ws/orders/{order_id}?token={token}")
            init_msg = ws.receive_json()
            if init_msg.get("event") == "connected":
                connected_count += 1
                clients.append(tc)
                websockets.append(ws)
        except Exception as e:
            connection_errors += 1
            
    connect_duration = time.time() - start_time
    print(f"Connected {connected_count}/{num_clients} clients in {connect_duration:.3f}s (Errors: {connection_errors})")
    
    # Verify manager active connections
    active_in_manager = len(connection_manager.active_connections.get(order_id, set()))
    print(f"Active connections tracked in ConnectionManager: {active_in_manager}")
    
    # 2. Broadcast a test message from client 0 and check if all other clients receive it
    broadcast_success = 0
    broadcast_errors = 0
    if websockets:
        test_ping = {
            "event": "delivery_location_ping",
            "latitude": 6.5244,
            "longitude": 3.3792,
            "timestamp": "2026-06-29T12:00:00Z"
        }
        b_start = time.time()
        websockets[0].send_json(test_ping)
        
        for idx, ws in enumerate(websockets):
            try:
                msg = ws.receive_json()
                if msg.get("event") == "delivery_location_ping":
                    broadcast_success += 1
            except Exception as e:
                broadcast_errors += 1
        b_duration = time.time() - b_start
        print(f"Broadcast received by {broadcast_success}/{len(websockets)} clients in {b_duration:.3f}s")
        
    # 3. Disconnect all clients
    for ws in websockets:
        try:
            ws.close()
        except Exception:
            pass
            
    remaining_in_manager = len(connection_manager.active_connections.get(order_id, set()))
    print(f"Remaining connections in manager after cleanup: {remaining_in_manager}")
    
    return {
        "num_requested": num_clients,
        "connected_count": connected_count,
        "connection_errors": connection_errors,
        "connect_duration_sec": connect_duration,
        "broadcast_success": broadcast_success,
        "remaining_in_manager": remaining_in_manager
    }

def run_rapid_location_pings_test(num_pings: int = 200) -> Dict[str, Any]:
    """
    Stress-tests rapid delivery location pings over a single WebSocket connection.
    Measures ping throughput, processing latency, and checks for message drops.
    """
    print(f"\n--- Running Rapid Location Pings Test ({num_pings} pings) ---")
    order_id = "ord_ping_stress"
    token = "test-valid-token"
    tc = TestClient(app)
    
    received_count = 0
    errors = 0
    start_time = time.time()
    
    with tc.websocket_connect(f"/ws/orders/{order_id}?token={token}") as ws:
        init_msg = ws.receive_json()
        
        send_start = time.time()
        for i in range(num_pings):
            ping = {
                "event": "delivery_location_ping",
                "latitude": 6.5000 + (i * 0.0001),
                "longitude": 3.3000 + (i * 0.0001),
                "timestamp": f"2026-06-29T12:00:{i%60:02d}Z"
            }
            ws.send_json(ping)
            resp = ws.receive_json()
            if resp.get("event") == "delivery_location_ping":
                received_count += 1
            else:
                errors += 1
        total_duration = time.time() - send_start
        
    pings_per_sec = received_count / total_duration if total_duration > 0 else 0
    print(f"Processed {received_count}/{num_pings} pings in {total_duration:.3f}s ({pings_per_sec:.1f} pings/sec)")
    
    return {
        "num_pings": num_pings,
        "received_count": received_count,
        "errors": errors,
        "duration_sec": total_duration,
        "pings_per_sec": pings_per_sec
    }

def run_auth_and_invalid_tokens_test() -> Dict[str, Any]:
    """
    Tests WebSocket authentication enforcement, invalid tokens, missing tokens, and backdoor tokens.
    """
    print("\n--- Running Auth & Invalid Tokens Test ---")
    results = {}
    tc = TestClient(app)
    
    # Case 1: Missing token
    try:
        with tc.websocket_connect("/ws/orders/ord_auth_1") as ws:
            ws.receive_json()
        results["missing_token"] = "FAILED (Accepted without token)"
    except Exception as e:
        results["missing_token"] = f"PASSED (Rejected as expected: {type(e).__name__})"
        
    # Case 2: Invalid token string
    try:
        with tc.websocket_connect("/ws/orders/ord_auth_1?token=invalid_random_token_999") as ws:
            ws.receive_json()
        results["invalid_token"] = "FAILED (Accepted invalid token)"
    except Exception as e:
        results["invalid_token"] = f"PASSED (Rejected invalid token: {type(e).__name__})"

    # Case 3: Backdoor test token prefix check ("test-anything")
    try:
        with tc.websocket_connect("/ws/orders/ord_auth_1?token=test-backdoor-user") as ws:
            msg = ws.receive_json()
            if msg.get("event") == "connected":
                results["backdoor_token_prefix"] = "VULNERABLE (Accepted token matching 'test-*' pattern)"
            else:
                results["backdoor_token_prefix"] = "REJECTED"
    except Exception as e:
        results["backdoor_token_prefix"] = f"REJECTED ({type(e).__name__})"

    # Case 4: Valid test token ("test-valid-token")
    try:
        with tc.websocket_connect("/ws/orders/ord_auth_1?token=test-valid-token") as ws:
            msg = ws.receive_json()
            if msg.get("event") == "connected":
                results["valid_test_token"] = "PASSED (Successfully connected)"
            else:
                results["valid_test_token"] = "FAILED"
    except Exception as e:
        results["valid_test_token"] = f"FAILED ({type(e).__name__})"
        
    for test_case, res in results.items():
        print(f"  {test_case}: {res}")
        
    return results

def run_malformed_payload_test() -> Dict[str, Any]:
    """
    Tests handling of malformed or invalid payloads (e.g., string latitude, missing fields, invalid JSON).
    """
    print("\n--- Running Malformed Payload & Error Handling Test ---")
    tc = TestClient(app)
    results = {}
    
    with tc.websocket_connect("/ws/orders/ord_err_1?token=test-valid-token") as ws:
        init_msg = ws.receive_json()
        
        # Test 1: Invalid latitude type (string "invalid_lat")
        bad_ping = {
            "event": "delivery_location_ping",
            "latitude": "invalid_lat",
            "longitude": 3.3792
        }
        ws.send_json(bad_ping)
        try:
            resp = ws.receive_json()
            results["invalid_latitude_type"] = f"HANDLED (Received response: {resp})"
        except Exception as e:
            results["invalid_latitude_type"] = f"CRASHED CONNECTION (Uncaught ValueError in server loop: {type(e).__name__})"

    # Test 2: Arbitrary unexpected event type
    try:
        with tc.websocket_connect("/ws/orders/ord_err_2?token=test-valid-token") as ws:
            ws.receive_json() # init
            ws.send_json({"event": "unknown_custom_event", "data": "hello"})
            resp = ws.receive_json()
            results["unknown_event_type"] = f"PASSED (Broadcasted to channel: {resp.get('event')})"
    except Exception as e:
        results["unknown_event_type"] = f"FAILED ({type(e).__name__})"

    for test_case, res in results.items():
        print(f"  {test_case}: {res}")

    return results

if __name__ == "__main__":
    print("==================================================")
    print("   WEBSOCKET REAL-TIME STRESS TEST HARNESS      ")
    print("==================================================")
    conn_res = run_concurrent_connections_test(50)
    ping_res = run_rapid_location_pings_test(200)
    auth_res = run_auth_and_invalid_tokens_test()
    malformed_res = run_malformed_payload_test()
    print("\n==================================================")
    print("               STRESS TEST SUMMARY                ")
    print("==================================================")
    print(json.dumps({
        "concurrent_connections": conn_res,
        "rapid_pings": ping_res,
        "auth_tests": auth_res,
        "malformed_tests": malformed_res
    }, indent=2))
