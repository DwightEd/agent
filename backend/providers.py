"""OpenAI-compatible chat/tool protocol, using only the Python standard library."""
import json
import os
import time
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    pass


class Provider:
    def __init__(self, config, on_usage, check_cancel):
        self.base = config['base_url'].rstrip('/')
        self.model = config['model']
        self.key = os.environ.get('LLM_API_KEY', '')
        self.config = config
        self.on_usage = on_usage
        self.check_cancel = check_cancel

    def complete(self, messages, tools):
        payload = {'model': self.model, 'messages': messages,
                   'max_tokens': self.config.get('max_output_tokens', 2500)}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        for attempt in range(3):
            self.check_cancel()
            # Count every outbound attempt, including failed requests, against the call cap.
            self.on_usage(None)
            request = urllib.request.Request(self.base + '/chat/completions',
                                             json.dumps(payload).encode(), headers, method='POST')
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ProviderError('模型响应超过 2 MB')
                result = json.loads(raw)
                message = result['choices'][0]['message']
                if not isinstance(message, dict):
                    raise ValueError('message must be an object')
                self.on_usage(result.get('usage') or {})
                return message
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ProviderError(f'模型 API 返回 HTTP {exc.code}；检查服务端密钥、模型名和配额') from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise ProviderError('模型 API 连接失败或 60 秒超时') from None
            except (KeyError, IndexError, TypeError, ValueError):
                raise ProviderError('模型 API 未返回有效的 chat/completions 消息') from None
            # Cancellation stays responsive between retries.
            for _ in range(5 * (attempt + 1)):
                self.check_cancel()
                time.sleep(0.2)


def extract_json(content):
    if not isinstance(content, str):
        raise ValueError('agent 必须返回 JSON 对象')
    text = content.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('agent 必须返回 JSON 对象')
    return value
