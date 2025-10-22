from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import asyncio
from typing import Dict, List, Optional
from datetime import datetime
import json
import os
import uuid

# Room management
rooms: Dict[str, List[WebSocket]] = {}
# Track assigned peer IDs for each room
room_peer_ids: Dict[str, List[int]] = {}
# Track peer connection states
room_peer_states: Dict[str, Dict[int, str]] = {}  # peer_id -> state
room_peer_last_heartbeat: Dict[str, Dict[int, datetime]] = {}
next_peer_id: Dict[str, int] = {}  # Track the next available peer ID for each room
room_lock = asyncio.Lock()
server_start_time = datetime.now()
connection_log: List[str] = []

# Connection states
PEER_STATE_CONNECTING = "connecting"
PEER_STATE_CONNECTED = "connected"
PEER_STATE_DISCONNECTED = "disconnected"

def log_event(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    connection_log.append(log_entry)
    if len(connection_log) > 50:
        connection_log.pop(0)

async def add_client_to_room(room_id: str, websocket: WebSocket) -> Optional[int]:
    """Add a client to a room and return assigned peer ID. Returns None if failed."""
    try:
        async with room_lock:
            if room_id not in rooms:
                rooms[room_id] = []
                room_peer_ids[room_id] = []
                room_peer_states[room_id] = {}
                room_peer_last_heartbeat[room_id] = {}
                next_peer_id[room_id] = 1  # Start with ID 1 (host) and increment
            
            current_count = len(rooms[room_id])
            log_event(f"DEBUG: Attempting to add client to room {room_id}, current count: {current_count}/4")
            
            if current_count >= 4:
                log_event(f"Room {room_id} is full - has {current_count} clients")
                return None
            
            # Assign a unique peer ID with room prefix
            assigned_id = next_peer_id[room_id]
            room_peer_ids[room_id].append(assigned_id)
            room_peer_states[room_id][assigned_id] = PEER_STATE_CONNECTING
            room_peer_last_heartbeat[room_id][assigned_id] = datetime.now()
            next_peer_id[room_id] += 1
            
            rooms[room_id].append(websocket)
            new_count = len(rooms[room_id])
            log_event(f"✅ Client added to room {room_id} as peer ID {assigned_id} ({new_count}/4)")
            return assigned_id
    except Exception as e:
        log_event(f"❌ Error adding client to room {room_id}: {e}")
        return None

async def remove_client_from_room(room_id: str, websocket: WebSocket):
    """Remove a client from a room and notify others."""
    try:
        async with room_lock:
            if room_id in rooms and websocket in rooms[room_id]:
                # Find the index of the websocket to remove the corresponding peer ID
                client_index = rooms[room_id].index(websocket)
                rooms[room_id].pop(client_index)
                
                # Remove the corresponding peer ID at the same index
                removed_peer_id = None
                if room_id in room_peer_ids and client_index < len(room_peer_ids[room_id]):
                    removed_peer_id = room_peer_ids[room_id].pop(client_index)
                
                # Remove from states and heartbeat tracking
                if removed_peer_id is not None and room_id in room_peer_states:
                    room_peer_states[room_id].pop(removed_peer_id, None)
                    room_peer_last_heartbeat[room_id].pop(removed_peer_id, None)
                
                if removed_peer_id is not None:
                    log_event(f"❌ Client (Peer ID {removed_peer_id}) removed from room {room_id} ({len(rooms[room_id])}/4 remaining)")
                    # Notify other peers about the disconnection
                    await notify_peer_disconnection(room_id, removed_peer_id)
                else:
                    log_event(f"❌ Client removed from room {room_id} ({len(rooms[room_id])}/4 remaining)")
                
                # Check if room is now empty and cleanup if needed
                if room_id in rooms and len(rooms[room_id]) == 0:
                    if room_id in rooms:
                        del rooms[room_id]
                    if room_id in room_peer_ids:
                        del room_peer_ids[room_id]
                    if room_id in room_peer_states:
                        del room_peer_states[room_id]
                    if room_id in room_peer_last_heartbeat:
                        del room_peer_last_heartbeat[room_id]
                    if room_id in next_peer_id:
                        del next_peer_id[room_id]
                    log_event(f"🧹 Room {room_id} cleaned up")
    except Exception as e:
        log_event(f"❌ Error removing client from room {room_id}: {e}")

async def notify_peer_disconnection(room_id: str, peer_id: int):
    """Notify all peers in room about a peer disconnection."""
    try:
        notification = {
            "type": "peer_disconnected",
            "peer_id": peer_id,
            "timestamp": datetime.now().isoformat()
        }
        await broadcast_to_room(room_id, json.dumps(notification))
        log_event(f"📢 Notified room {room_id} about peer {peer_id} disconnection")
    except Exception as e:
        log_event(f"❌ Error notifying peer disconnection: {e}")

async def update_peer_state(room_id: str, peer_id: int, state: str):
    """Update the state of a peer."""
    async with room_lock:
        if room_id in room_peer_states and peer_id in room_peer_states[room_id]:
            room_peer_states[room_id][peer_id] = state
            room_peer_last_heartbeat[room_id][peer_id] = datetime.now()
            log_event(f"📊 Peer {peer_id} in room {room_id} state changed to {state}")

async def relay_message(room_id: str, message: str, sender: WebSocket):
    """Relay messages between peers with enhanced handling."""
    try:
        async with room_lock:
            if room_id in rooms:
                # Find sender's peer ID
                sender_peer_id = None
                if room_id in room_peer_ids:
                    sender_index = rooms[room_id].index(sender)
                    if sender_index < len(room_peer_ids[room_id]):
                        sender_peer_id = room_peer_ids[room_id][sender_index]
                
                # Parse the message to check if it's a special request
                try:
                    msg_data = json.loads(message)
                    msg_type = msg_data.get("type")
                    
                    # Handle heartbeat messages
                    if msg_type == "heartbeat":
                        if sender_peer_id:
                            await update_peer_state(room_id, sender_peer_id, PEER_STATE_CONNECTED)
                        return
                    
                    # Handle special message types
                    elif msg_type == "get_peers":
                        # Send back the list of connected peer IDs and their states
                        if room_id in room_peer_ids:
                            peer_list = []
                            for i, peer_id in enumerate(room_peer_ids[room_id]):
                                peer_list.append({
                                    "id": peer_id,
                                    "state": room_peer_states[room_id].get(peer_id, "unknown")
                                })
                            peer_msg = {
                                "type": "peer_list",
                                "peers": peer_list,
                                "timestamp": datetime.now().isoformat()
                            }
                            try:
                                await sender.send_text(json.dumps(peer_msg))
                                log_event(f"📤 Sent peer list to peer {sender_peer_id} in room {room_id}")
                            except Exception as e:
                                log_event(f"⚠️ Error sending peer list: {e}")
                        return
                    
                    elif msg_type == "new_peer_joined":
                        # Broadcast new peer announcement and updated peer list in one message
                        if room_id in room_peer_ids:
                            peer_list = []
                            for i, peer_id in enumerate(room_peer_ids[room_id]):
                                peer_list.append({
                                    "id": peer_id,
                                    "state": room_peer_states[room_id].get(peer_id, "unknown")
                                })
                            response = {
                                "type": "peer_update",
                                "announcement": msg_data,
                                "peers": peer_list,
                                "timestamp": datetime.now().isoformat()
                            }
                            await broadcast_to_room(room_id, json.dumps(response), sender)
                            log_event(f"📢 Sent peer update to all peers in room {room_id}")
                        return
                    
                    # For regular messages (WebRTC SDP offers/answers and ICE candidates)
                    else:
                        # Add sender peer ID to the outer message for tracking
                        original_message = json.loads(message)
                        if sender_peer_id:
                            original_message["from_peer_id"] = sender_peer_id
                        
                        # Check for a 'to_peer_id' at the root level to enable direct messaging
                        to_peer_id = original_message.get("to_peer_id")
                        if to_peer_id:
                            await send_to_peer(room_id, to_peer_id, json.dumps(original_message))
                            log_event(f"🎯 Relayed message from {sender_peer_id} to {to_peer_id} in room {room_id}")
                        else:
                            # Process the inner data and add sender info
                            if isinstance(msg_data, dict):
                                msg_data["from_peer_id"] = sender_peer_id
                            
                            # Fallback to broadcast if no specific recipient
                            await broadcast_to_room(room_id, json.dumps(original_message), sender)
                            if "candidate" not in msg_data and "name" not in msg_data:
                                log_event(f"📤 Relayed {msg_type} from peer {sender_peer_id} in room {room_id}")
                
                except json.JSONDecodeError:
                    # If not JSON, treat as regular message and broadcast to all others
                    await broadcast_to_room(room_id, message, sender)
                    log_event(f"📤 Relayed message from peer {sender_peer_id} in room {room_id}")
    
    except Exception as e:
        log_event(f"❌ Error in relay_message: {e}")

async def broadcast_to_room(room_id: str, message: str, exclude_client: WebSocket = None):
    """Broadcast a message to all clients in the room."""
    try:
        async with room_lock:
            if room_id in rooms:
                for client in rooms[room_id]:
                    if exclude_client is None or client != exclude_client:
                        try:
                            await client.send_text(message)
                        except Exception as e:
                            log_event(f"⚠️ Broadcast error: {e}")
    except Exception as e:
        log_event(f"❌ Error in broadcast_to_room: {e}")

async def send_to_peer(room_id: str, peer_id: int, message: str):
    """Send a message to a specific peer in a room."""
    try:
        async with room_lock:
            if room_id in rooms and room_id in room_peer_ids:
                if peer_id in room_peer_ids[room_id]:
                    peer_index = room_peer_ids[room_id].index(peer_id)
                    if peer_index < len(rooms[room_id]):
                        recipient_ws = rooms[room_id][peer_index]
                        try:
                            await recipient_ws.send_text(message)
                        except Exception as e:
                            log_event(f"⚠️ Error sending to peer {peer_id}: {e}")
    except Exception as e:
        log_event(f"❌ Error in send_to_peer: {e}")

async def check_connection_health():
    """Periodically check connection health and cleanup dead connections."""
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds
        
        try:
            async with room_lock:
                current_time = datetime.now()
                room_ids = list(rooms.keys())
                
                for room_id in room_ids:
                    if room_id in room_peer_last_heartbeat:
                        dead_peers = []
                        for peer_id, last_heartbeat in room_peer_last_heartbeat[room_id].items():
                            # If no heartbeat for 60 seconds, consider peer dead
                            if (current_time - last_heartbeat).total_seconds() > 60:
                                dead_peers.append(peer_id)
                        
                        # Remove dead peers
                        for peer_id in dead_peers:
                            log_event(f"💀 Peer {peer_id} in room {room_id} appears dead, removing...")
                            # Find and remove the corresponding websocket
                            if room_id in room_peer_ids and peer_id in room_peer_ids[room_id]:
                                peer_index = room_peer_ids[room_id].index(peer_id)
                                if peer_index < len(rooms[room_id]):
                                    dead_websocket = rooms[room_id][peer_index]
                                    try:
                                        await dead_websocket.close()
                                    except:
                                        pass
                                    # Trigger cleanup
                                    await remove_client_from_room(room_id, dead_websocket)
        
        except Exception as e:
            log_event(f"⚠️ Error in health check: {e}")

# Create FastAPI app
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    """Start background tasks when the app starts."""
    # Start the health check task
    asyncio.create_task(check_connection_health())
    log_event("🏥 Health check task started")

@app.get("/")
async def root():
    """Home page with server info."""
    uptime = datetime.now() - server_start_time
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    
    # Clean up any empty rooms that might have been left behind
    try:
        async with room_lock:
            room_ids = list(rooms.keys())
            for room_id in room_ids:
                if room_id in rooms and len(rooms[room_id]) == 0:
                    if room_id in rooms:
                        del rooms[room_id]
                    if room_id in room_peer_ids:
                        del room_peer_ids[room_id]
                    if room_id in room_peer_states:
                        del room_peer_states[room_id]
                    if room_id in room_peer_last_heartbeat:
                        del room_peer_last_heartbeat[room_id]
                    if room_id in next_peer_id:
                        del next_peer_id[room_id]
                    log_event(f"🧹 Cleanup: Empty room {room_id} removed")
    except Exception as e:
        log_event(f"⚠️ Error during room cleanup: {e}")
    
    room_info = ""
    if rooms:
        room_info = "<h3>Active Rooms:</h3><ul>"
        for room_id, clients in rooms.items():
            room_info += f"<li>Room <code>{room_id}</code>: {len(clients)}/4 players"
            if room_id in room_peer_states:
                connected_count = sum(1 for state in room_peer_states[room_id].values() if state == PEER_STATE_CONNECTED)
                room_info += f" ({connected_count} connected)"
            room_info += "</li>"
        room_info += "</ul>"
    else:
        room_info = "<p><em>No active rooms</em></p>"
    
    log_info = "<h3>Recent Events:</h3><ul>"
    for log in reversed(connection_log[-15:]):
        log_info += f"<li><code>{log}</code></li>"
    log_info += "</ul>"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>WebRTC Signaling Server</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 900px;
                margin: 0 auto;
                padding: 20px;
                background: #0f172a;
                color: #e2e8f0;
            }}
            h1 {{ color: #60a5fa; }}
            h2 {{ color: #34d399; margin-top: 30px; }}
            h3 {{ color: #a78bfa; }}
            code {{
                background: #1e293b;
                padding: 2px 6px;
                border-radius: 4px;
                color: #fbbf24;
            }}
            pre {{
                background: #1e293b;
                padding: 15px;
                border-radius: 8px;
                overflow-x: auto;
                border-left: 4px solid #60a5fa;
            }}
            .status {{
                display: inline-block;
                padding: 8px 16px;
                background: #065f46;
                border-radius: 20px;
                color: #d1fae5;
                margin: 10px 0;
            }}
            ul {{
                background: #1e293b;
                padding: 15px 30px;
                border-radius: 8px;
            }}
            li {{ margin: 8px 0; }}
            .endpoint {{
                background: #1e293b;
                padding: 15px;
                border-radius: 8px;
                border-left: 4px solid #34d399;
                margin: 15px 0;
            }}
            .health-indicator {{
                display: inline-block;
                width: 10px;
                height: 10px;
                background: #34d399;
                border-radius: 50%;
                margin-right: 8px;
            }}
        </style>
        <script>
            // Auto-refresh every 3 seconds
            setTimeout(() => location.reload(), 3000);
        </script>
    </head>
    <body>
        <h1>🎮 WebRTC Signaling Server</h1>
        <div class="status">🟢 <span class="health-indicator"></span>Server Online | Uptime: {hours}h {minutes}m</div>
        
        <h2>📡 Connection Information</h2>
        <div class="endpoint">
            <strong>WebSocket Endpoint:</strong><br>
            <code>wss://web-services-nheh.onrender.com/ws/{{room_id}}/</code>
        </div>
        
        <h2>🔧 Godot Usage</h2>
        <pre><code>var signaling_url = "wss://web-services-nheh.onrender.com/ws/"
var room_id = "test123"
websocket_peer.connect_to_url(signaling_url + room_id + "/")</code></pre>
        
        <h2>📊 Server Status</h2>
        <p><strong>Active Rooms:</strong> {len(rooms)}</p>
        <p><strong>Total Connections:</strong> {sum(len(clients) for clients in rooms.values())}</p>
        {room_info}
        
        {log_info}
        
        <h3>🏥 Health Monitoring</h3>
        <p><em>• Connection health checks every 30 seconds</em></p>
        <p><em>• Dead connections automatically cleaned up after 60 seconds</em></p>
        <p><em>• Peer state tracking and disconnection notifications</em></p>
        
        <p style="margin-top: 30px; color: #64748b; font-size: 0.9em;">
            <em>Page auto-refreshes every 3 seconds</em>
        </p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/health")
async def health_check():
    """Health check endpoint for Render."""
    return {
        "status": "healthy",
        "uptime_seconds": (datetime.now() - server_start_time).total_seconds(),
        "active_rooms": len(rooms),
        "total_connections": sum(len(clients) for clients in rooms.values()),
        "timestamp": datetime.now().isoformat()
    }

@app.websocket("/ws/{room_id}/")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """WebSocket endpoint for signaling with enhanced error handling."""
    log_event(f"🔌 Connection attempt for room: {room_id}")
    
    try:
        # Accept WebSocket connection
        await websocket.accept()
        log_event(f"✅ WebSocket accepted for room: {room_id}")
    except Exception as e:
        log_event(f"❌ Error accepting WebSocket: {e}")
        return
    
    # Add client to room and get peer ID
    assigned_id = await add_client_to_room(room_id, websocket)
    if assigned_id is None:
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Room is full",
                "code": "ROOM_FULL"
            }))
            await websocket.close()
        except:
            pass
        return
    
    if assigned_id is None:
        log_event(f"❌ Could not determine assigned peer ID for websocket in room {room_id}")
        return
    
    # Update peer connection state to connected
    await update_peer_state(room_id, assigned_id, PEER_STATE_CONNECTED)
    
    # Send welcome message with assigned peer ID
    try:
        welcome_msg = {
            "type": "welcome",
            "peer_id": assigned_id,
            "room_id": room_id,
            "timestamp": datetime.now().isoformat()
        }
        await websocket.send_text(json.dumps(welcome_msg))
        log_event(f"🎉 Welcome message sent to peer {assigned_id}")
    except Exception as e:
        log_event(f"❌ Error sending welcome message: {e}")
        return
    
    try:
        while True:
            message = await websocket.receive_text()
            await relay_message(room_id, message, websocket)
    
    except WebSocketDisconnect:
        log_event(f"🔌 Client (Peer {assigned_id}) disconnected normally from room: {room_id}")
    except Exception as e:
        log_event(f"⚠️ WebSocket error for peer {assigned_id} in room {room_id}: {e}")
    finally:
        await remove_client_from_room(room_id, websocket)

log_event("🚀 Server initialized and ready")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
