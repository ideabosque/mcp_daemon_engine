#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import asyncio
import logging
from collections import deque
from itertools import count
from typing import Any, Dict, Set, Tuple, Optional


class SSEManager:
    """Thread-safe SSE client manager with proper lifecycle management"""
    
    def __init__(self, max_history: int = 1000, max_queue_size: int = 100):
        self._clients: Dict[int, asyncio.Queue] = {}
        self._user_clients: Dict[str, Set[int]] = {}
        self._lock = asyncio.Lock()
        self._message_history: deque = deque(maxlen=max_history)
        self._client_id_seq = count(1)
        self._message_id_seq = count(1)
        self._max_queue_size = max_queue_size
        self._logger = logging.getLogger(__name__)
    
    async def add_client(self, username: str) -> Tuple[int, asyncio.Queue]:
        """Add a new SSE client and return client_id and queue"""
        async with self._lock:
            client_id = next(self._client_id_seq)
            queue = asyncio.Queue(maxsize=self._max_queue_size)
            self._clients[client_id] = queue
            self._user_clients.setdefault(username, set()).add(client_id)
            self._logger.info(f"Added SSE client {client_id} for user {username}")
            return client_id, queue
    
    async def remove_client(self, client_id: int, username: str) -> bool:
        """Remove a client and cleanup associated data"""
        async with self._lock:
            removed = self._clients.pop(client_id, None) is not None
            
            if username in self._user_clients:
                self._user_clients[username].discard(client_id)
                if not self._user_clients[username]:
                    del self._user_clients[username]
            
            if removed:
                self._logger.info(f"Removed SSE client {client_id} for user {username}")
            
            return removed
    
    async def get_clients_for_user(self, username: str) -> Set[int]:
        """Get all client IDs for a specific user"""
        async with self._lock:
            return self._user_clients.get(username, set()).copy()
    
    async def broadcast_message(self, message: Dict[str, Any]) -> int:
        """Broadcast message to all clients and return success count"""
        message_id = next(self._message_id_seq)
        message_with_id = dict(message, id=message_id)
        self._message_history.append(message_with_id)
        
        success_count = 0
        dead_clients = []
        
        async with self._lock:
            for client_id, queue in list(self._clients.items()):
                try:
                    queue.put_nowait(message_with_id)
                    success_count += 1
                except asyncio.QueueFull:
                    self._logger.warning(f"Queue full for client {client_id}, marking for removal")
                    dead_clients.append(client_id)
                except Exception as e:
                    self._logger.error(f"Error broadcasting to client {client_id}: {e}")
                    dead_clients.append(client_id)
        
        # Clean up dead clients
        for cid in dead_clients:
            await self._cleanup_dead_client(cid)
        
        self._logger.debug(f"Broadcast message to {success_count} clients, removed {len(dead_clients)} dead clients")
        return success_count
    
    async def send_to_client(self, client_id: int, message: Dict[str, Any]) -> bool:
        """Send message to a specific client"""
        message_id = next(self._message_id_seq)
        message_with_id = dict(message, id=message_id)
        self._message_history.append(message_with_id)
        
        async with self._lock:
            queue = self._clients.get(client_id)
            if not queue:
                return False
            
            try:
                queue.put_nowait(message_with_id)
                return True
            except asyncio.QueueFull:
                self._logger.warning(f"Queue full for client {client_id}, removing")
                await self._cleanup_dead_client(client_id)
                return False
            except Exception as e:
                self._logger.error(f"Error sending to client {client_id}: {e}")
                await self._cleanup_dead_client(client_id)
                return False
    
    async def send_to_user(self, username: str, message: Dict[str, Any]) -> bool:
        """Send message to all clients of a specific user"""
        client_ids = await self.get_clients_for_user(username)
        if not client_ids:
            return False
        
        delivered = False
        for client_id in client_ids:
            success = await self.send_to_client(client_id, message)
            delivered = delivered or success
        
        return delivered
    
    async def get_missed_messages(self, last_event_id: Optional[str]) -> list:
        """Get messages missed since last_event_id"""
        if not last_event_id or not last_event_id.isdigit():
            return []
        
        last_id = int(last_event_id)
        return [msg for msg in self._message_history if msg.get("id", 0) > last_id]
    
    async def _cleanup_dead_client(self, client_id: int):
        """Internal method to clean up a dead client"""
        self._clients.pop(client_id, None)
        
        # Remove from user mappings
        for username, client_set in list(self._user_clients.items()):
            client_set.discard(client_id)
            if not client_set:
                del self._user_clients[username]
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get SSE manager statistics"""
        async with self._lock:
            user_distribution = {
                username: len(client_ids) 
                for username, client_ids in self._user_clients.items()
            }
            
            return {
                "total_clients": len(self._clients),
                "total_users": len(self._user_clients),
                "user_distribution": user_distribution,
                "message_history_size": len(self._message_history),
                "max_queue_size": self._max_queue_size,
            }
    
    async def cleanup_all(self):
        """Cleanup all clients and resources"""
        async with self._lock:
            self._clients.clear()
            self._user_clients.clear()
            self._message_history.clear()
            self._logger.info("Cleaned up all SSE clients and resources")


# Global SSE manager instance
sse_manager = SSEManager()