import asyncio
import sys

from openai import APIConnectionError, APIError, APIStatusError


def is_retryable(error: APIError) -> bool:
    if isinstance(error, APIConnectionError):
        return True

    if isinstance(error, APIStatusError):
        status = error.status_code
        return status in {408, 409, 429} or 500 <= status <= 599

    return False


async def with_retry(operation, max_attempts=3, base_delay=1):
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()

        except APIError as error:
            if not is_retryable(error) or attempt == max_attempts:
                raise

            delay = base_delay * 2 ** (attempt - 1)
            print(
                f"请求失败：{type(error).__name__}，"
                f"{delay} 秒后重试（下一次：{attempt + 1}/{max_attempts}）",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)