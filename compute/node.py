import math
import random
import threading
import time

from mpmath import mp


class Node:
    def __init__(self, value, id, dps, timeWindow):
        self.value = value
        self.id = id
        self.dps = dps
        self.active = False
        self.forwards = {}
        self._lock = threading.Lock()
        self.timeWindow = timeWindow

    def computeValue(self, weight):
        mp.dps = self.dps
        x = self.value + weight
        self.value = 1 / (1 + mp.exp(-x))

    def computeId(self, manager):
        mp.dps = self.dps
        target_id = int(mp.sin(mp.pi * self.value) * 10 ** self.dps)
        if manager.dopamine > 0:
            noise = int(random.uniform(-1, 1) * math.sqrt(manager.dopamine) * 10 ** self.dps)
            target_id += noise
        self.forwards[target_id] = self.forwards.get(target_id, 0) + 1

    def start(self, manager, weight):
        if not manager._enter_worker():
            return
        try:
            with self._lock:
                if manager.stopped:
                    return
                if self.active:
                    self.computeValue(weight)
                    return
                self.active = True
                self.computeValue(weight)

            # Keep each activation's original independent sleeping thread.
            time.sleep(self.timeWindow)

            with self._lock:
                if manager.stopped:
                    self.active = False
                    return
                self.computeId(manager)
                targets = dict(self.forwards)
                self.active = False

            manager.saveNodeToDatabase(self.id)
            self.forward(manager, targets)
            if not manager.stopped and self.id in manager.callbacks:
                manager.callbacks[self.id](self)
        finally:
            manager._leave_worker()

    def forward(self, manager, targets):
        value = self.value
        for target_id, weight in targets.items():
            if manager.stopped:
                break
            if random.random() < min(1.0, max(0.0, float(weight))):
                threading.Thread(
                    target=manager.getNode(target_id).start,
                    args=(manager, value),
                    daemon=True,
                ).start()
