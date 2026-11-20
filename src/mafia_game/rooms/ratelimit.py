"""A token bucket: the classic way to limit how fast someone can send messages.

Picture a bucket that holds up to `capacity` tokens and refills at a steady
rate. Each message spends one token. With tokens left, a burst of quick taps
is fine; once it's empty, messages are refused until it refills. So short
bursts are allowed, but the average rate can never go above the refill rate.
"""


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float, now_ms: int) -> None:
        self.capacity = capacity
        self.refill_per_ms = refill_per_second / 1000
        self.tokens = float(capacity)  # start full
        self.updated_at = now_ms

    def allow(self, now_ms: int) -> bool:
        # Add the tokens earned since last time, never more than the bucket holds.
        elapsed = max(0, now_ms - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_ms)
        self.updated_at = now_ms
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False
