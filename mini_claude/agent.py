from .client import ask_model


class Agent:
    def __init__(self):
        self.messages = []

    async def chat(self, msg: str) -> str:
        user_message = {"role": "user", "content": msg}
        request_messages = self.messages + [user_message]

        answer = await ask_model(request_messages)

        self.messages.append(user_message)
        self.messages.append(
            {"role": "assistant", "content": answer}
        )

        return answer

    def clear_history(self):
        self.messages.clear()