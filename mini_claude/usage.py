class UsageStats:
    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

        self.reported_requests = 0
        self.missing_requests = 0

    def record(self, usage):
        if usage is None:
            self.missing_requests += 1
            return

        self.reported_requests += 1

        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens