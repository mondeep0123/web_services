import gradio as gr
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from typing import Dict, List
from datetime import datetime
import json

# Room management
rooms: Dict[str, List[WebSocket]] = {}
room_lock = asyncio.Lock()

# Server stats
server_start_time = datetime.now()
connection_log: List[str] = []

def log_event(message: str):
    """Log events with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    connection_log.append(log_entry)
    # Keep only last 50 log entries
    if len(connection_log) > 50:
        connection_log.pop(0)

async def add_client_to_room(room_id: str, websocket: WebSocket) -> bool:
    """Add a client to a room. Returns True if successful, False if room is full."""
    async with room_lock:
        if room_id not in rooms:
            rooms[room_id] = []
        
        if len(rooms[room_id]) >= 2:
            log_event(f"Room {room_id} is full, rejecting connection")
            return False
        
        rooms[room_id].append(websocket)
        log_event(f"Client added to room {room_id} ({len(rooms[room_id])}/2)")
        return True

async def remove_client_from_room(room_id: str, websocket: WebSocket):
    """Remove a client from a room and clean up empty rooms."""
    async with room_lock:
        if room_id in rooms:
            if websocket in rooms[room_id]:
                rooms[room_id].remove(websocket)
                log_event(f"Client removed from room {room_id} ({len(rooms[room_id])}/2 remaining)")
            
            # Clean up empty rooms
            if len(rooms[room_id]) == 0:
                del rooms[room_id]
                log_event(f"Room {room_id} cleaned up (empty)")

async def relay_message(room_id: str, message: str, sender: WebSocket):
    """Relay a message to the other client in the room."""
    async with room_lock:
        if room_id in rooms:
            for client in rooms[room_id]:
                if client != sender:
                    try:
                        await client.send_text(message)
                        # Parse message type for logging
                        try:
                            msg_data = json.loads(message)
                            msg_type = msg_data.get("type", "unknown")
                            log_event(f"Relayed {msg_type} in room {room_id}")
                        except:
                            log_event(f"Relayed message in room {room_id}")
                    except Exception as e:
                        log_event(f"Error relaying message in room {room_id}: {e}")

def get_room_status():
    """Get current room status for UI display."""
    uptime = datetime.now() - server_start_time
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    
    status = f"🟢 **Server Online** | Uptime: {hours}h {minutes}m\n\n"
    status += f"**Active Rooms: {len(rooms)}**\n\n"
    
    if rooms:
        status += "| Room ID | Players |\n"
        status += "|---------|--------|\n"
        for room_id, clients in rooms.items():
            status += f"| {room_id} | {len(clients)}/2 |\n"
    else:
        status += "*No active rooms*"
    
    return status

def get_connection_log():
    """Get recent connection log."""
    if not connection_log:
        return "*No events yet*"
    
    # Return last 20 entries in reverse order (most recent first)
    return "\n".join(reversed(connection_log[-20:]))

# Create Gradio Interface FIRST
with gr.Blocks(title="WebRTC Signaling Server") as demo:
    gr.Markdown("# 🎮 WebRTC Signaling Server for Godot")
    gr.Markdown("This server facilitates WebRTC peer connections between Godot game clients.")
    
    with gr.Row():
        with gr.Column():
            gr.Markdown("### 📊 Server Status")
            status_display = gr.Markdown(get_room_status())
        
        with gr.Column():
            gr.Markdown("### 📝 Connection Log")
            log_display = gr.Markdown(get_connection_log())
    
    gr.Markdown("""
    ### 📡 Connection Information
    
    **WebSocket Endpoint:** `wss://your-render-app-name.onrender.com/ws/{room_id}/`
    
    - Each room supports a maximum of 2 clients
    - Messages are relayed verbatim between peers
    - Rooms are automatically cleaned up when empty
    
    ### 🔧 Usage in Godot
    
    ```gdscript
    var signaling_url = "wss://your-render-app-name.onrender.com/ws/"
    var room_id = "my_game_room"
    websocket_peer.connect_to_url(signaling_url + room_id + "/")
    ```
    
    ### 🐛 Troubleshooting
    
    If connections fail:
    1. Check the Connection Log above for error messages
    2. Verify the room ID matches between clients
    3. Ensure only 2 clients try to join the same room
    4. Check Godot console for SSL/certificate errors
    """)
    
    # Update status every 2 seconds using gr.Timer
    timer = gr.Timer(value=2)
    timer.tick(
        fn=lambda: (get_room_status(), get_connection_log()),
        inputs=None,
        outputs=[status_display, log_display]
    )

# Create FastAPI app AFTER Gradio interface
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws/{room_id}/")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """WebSocket endpoint for signaling."""
    log_event(f"WebSocket connection attempt for room: {room_id}")
    
    try:
        await websocket.accept()
        log_event(f"WebSocket accepted for room: {room_id}")
    except Exception as e:
        log_event(f"Error accepting WebSocket for room {room_id}: {e}")
        return
    
    # Try to add client to room
    if not await add_client_to_room(room_id, websocket):
        try:
            await websocket.send_text('{"error": "Room is full"}')
            await websocket.close()
        except:
            pass
        return
    
    try:
        while True:
            # Receive message from client
            message = await websocket.receive_text()
            
            # Relay message to other client in the room
            await relay_message(room_id, message, websocket)
            
    except WebSocketDisconnect:
        log_event(f"Client disconnected normally from room: {room_id}")
    except Exception as e:
        log_event(f"Error in WebSocket connection for room {room_id}: {e}")
    finally:
        await remove_client_from_room(room_id, websocket)

# Mount Gradio app on FastAPI
app = gr.mount_gradio_app(app, demo, path="/")

log_event("Server initialized and ready")
