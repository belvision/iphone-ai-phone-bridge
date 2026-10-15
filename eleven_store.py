"""Private ElevenLabs/LLM configuration; API secrets never returned to the UI."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit
from connection_store import protected

PROMPT = ('Ты виртуальный помощник Сергея Гаева по автоматизации бизнеса. '
          'Говори исключительно по-русски, спокойно и кратко, по 1–2 предложения. '
          'Задавай один вопрос за раз. Выясни сферу бизнеса, задачу автоматизации, '
          'текущий процесс и желаемые сроки. Не выдумывай цены, возможности, сроки '
          'или выполненные действия. Если просят подождать, ответь кратко и жди. '
          'Если собеседник прощается, попрощайся. Не называй себя человеком.')
DEFAULTS = {'agentId': '', 'voiceId': '', 'ttsModel': 'eleven_flash_v2_5',
            'llmUrl': '', 'llmModel': '', 'prompt': PROMPT,
            'greeting': 'Здравствуйте! Я виртуальный помощник Сергея Гаева. Какую задачу автоматизации вы хотите решить?',
            'elevenKey': '', 'llmKey': '', 'replyPauseMs': 350}


def validate(value):
    result = dict(DEFAULTS)
    for key in result:
        if key == 'replyPauseMs': continue
        item = value.get(key, result[key])
        if not isinstance(item, str) or len(item) > (12000 if key == 'prompt' else 2048):
            raise ValueError('Некорректное поле подключения: ' + key)
        result[key] = item.strip()
    pause = value.get('replyPauseMs', DEFAULTS['replyPauseMs'])
    if isinstance(pause, bool) or not isinstance(pause, int) or not 250 <= pause <= 1500:
        raise ValueError('Пауза перед ответом должна быть от 250 до 1500 мс.')
    result['replyPauseMs'] = pause
    for key in ('agentId', 'voiceId', 'ttsModel'):
        if result[key] and not re.fullmatch(r'[A-Za-z0-9_-]{1,240}', result[key]):
            raise ValueError('Проверьте ' + key)
    if any('\n' in result[k] or '\r' in result[k] for k in ('elevenKey', 'llmKey')):
        raise ValueError('Некорректный API-ключ.')
    if result['llmUrl']:
        url = urlsplit(result['llmUrl'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.path.endswith('/chat/completions')):
            raise ValueError('Нужен HTTPS URL модели, заканчивающийся на /chat/completions.')
    if not result['prompt'] or not result['greeting']:
        raise ValueError('Заполните инструкцию и приветствие.')
    return result


def public_config(value):
    result = {k: v for k, v in value.items() if k not in ('elevenKey', 'llmKey')}
    result.update(hasElevenKey=bool(value['elevenKey']), hasLlmKey=bool(value['llmKey']))
    result['agentsReady'] = bool(value['elevenKey'] and value['agentId'])
    result['customReady'] = bool(value['elevenKey'] and value['voiceId'] and value['llmUrl'] and value['llmModel'] and value['llmKey'])
    return result


class ElevenStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ['LOCALAPPDATA']) / 'RetellPhoneBridge' / 'eleven.dpapi'
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.is_file(): return dict(DEFAULTS)
            data = self.path.read_bytes()
            if len(data) > 65536: raise ValueError('Некорректное подключение ElevenLabs.')
            return validate(json.loads(protected(data, decrypt=True)))

    def save(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise ValueError('Неизвестные настройки ElevenLabs.')
        with self.lock:
            value = self.read()
            # Empty password inputs preserve an existing encrypted secret.
            value.update({k: v for k, v in changes.items() if k not in ('elevenKey', 'llmKey') or v})
            value = validate(value)
            data = protected(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='eleven-', delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary: temporary.unlink(missing_ok=True)
            return public_config(value)
