from mafia_game.rooms.ratelimit import TokenBucket


def test_allows_a_burst_then_refuses():
    bucket = TokenBucket(capacity=3, refill_per_second=1, now_ms=0)
    assert [bucket.allow(0) for _ in range(4)] == [True, True, True, False]


def test_refills_over_time():
    bucket = TokenBucket(capacity=3, refill_per_second=1, now_ms=0)
    for _ in range(3):
        bucket.allow(0)

    assert not bucket.allow(500)  # only half a token so far
    assert bucket.allow(1_000)  # one whole token
    assert not bucket.allow(1_000)  # ...and it's spent


def test_never_holds_more_than_capacity():
    bucket = TokenBucket(capacity=2, refill_per_second=1, now_ms=0)
    # A minute of quiet earns 60 tokens, but the bucket only holds 2.
    assert [bucket.allow(60_000) for _ in range(3)] == [True, True, False]
