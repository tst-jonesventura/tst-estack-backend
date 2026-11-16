import asyncio
from typing import Dict, Set
import threading

class Broadcaster:
    def __init__(self):
        self.subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic: str) -> asyncio.Queue:
        with self._lock:
            if topic not in self.subscribers:
                self.subscribers[topic] = set()
            queue = asyncio.Queue()
            self.subscribers[topic].add(queue)
            return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue):
        with self._lock:
            if topic in self.subscribers:
                self.subscribers[topic].discard(queue)
                if not self.subscribers[topic]:
                    del self.subscribers[topic]

    async def broadcast(self, topic: str, message: any):
        queues_to_broadcast = []
        with self._lock:
            if topic in self.subscribers:
                queues_to_broadcast = list(self.subscribers[topic])
        
        for queue in queues_to_broadcast:
            await queue.put(message)

broadcaster = Broadcaster()
