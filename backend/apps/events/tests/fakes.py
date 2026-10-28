class FakePublisher:
    """Records messages; `fail_from` makes message N (0-based, cumulative) onward fail."""

    def __init__(self, fail_from=None, error="broker down"):
        self.sent = []
        self.fail_from = fail_from
        self.error = error
        self.calls = 0

    def publish(self, messages, *, timeout):
        self.calls += 1
        results = []
        for m in messages:
            if self.fail_from is not None and len(self.sent) >= self.fail_from:
                results.append(self.error)
                results += [self.error] * (len(messages) - len(results))
                break
            self.sent.append(m)
            results.append(None)
        return results

    def produce_nowait(self, message):
        self.sent.append(message)
        return True
