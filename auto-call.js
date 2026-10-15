// Explicitly enabled sequential-call gate: no dialing, phone numbers,
// Retell requests, or credentials. Each sequence can start at most once.
const PHASES = new Set(['idle', 'dialing', 'incoming', 'connected', 'held', 'ended', 'lost', 'multiple', 'unknown']);
export function trustedPhoneState(state) {
  return !!state && state.available === true && typeof state.active === 'boolean'
    && PHASES.has(state.callPhase) && Number.isSafeInteger(state.callSequence) && state.callSequence >= 0
    && typeof state.audioOnComputer === 'boolean' && Number.isFinite(state.ageMs) && state.ageMs >= 0 && state.ageMs <= 2000;
}
export function answeredOnComputer(state) {
  return trustedPhoneState(state) && state.callPhase === 'connected' && state.active && state.audioOnComputer;
}
export function describePhoneState(state) {
  if (!state?.available) return 'Наблюдение за звонком недоступно.';
  if (!trustedPhoneState(state)) return 'Состояние звонка ещё не подтверждено или устарело.';
  return {
    idle: 'Активного звонка нет.', dialing: 'Идёт набор; ожидаю ответа.', incoming: 'Входящий звонок; ожидаю ответа.',
    connected: state.audioOnComputer ? 'Собеседник ответил; разговор на компьютере.' : 'Собеседник ответил; звук на телефоне.',
    held: 'Звонок на удержании.', ended: 'Звонок завершён.', lost: 'Связь со звонком потеряна.',
    multiple: 'Обнаружено несколько звонков.', unknown: 'Состояние звонка не определено.',
  }[state.callPhase];
}
export class AutoCallGate {
  constructor() { this.cancel(); }
  cancel() {
    this.mode = 'idle'; this.sequence = null; this.baseline = null;
    this.allowBaseline = false; this.automatic = false; this.uncertainSince = null;
  }
  arm(state) {
    if (!trustedPhoneState(state)) throw new Error('Не удалось подтвердить состояние телефона. Ожидание не включено.');
    this.cancel(); this.mode = 'armed'; this.automatic = true; this.baseline = state.callSequence;
    this.allowBaseline = ['dialing', 'incoming', 'connected', 'held', 'unknown'].includes(state.callPhase);
  }
  startManual(state) {
    if (!answeredOnComputer(state)) throw new Error('Для запуска дождитесь ответа и оставьте разговор на компьютере.');
    this.cancel(); this.mode = 'starting'; this.sequence = state.callSequence;
    return this.sequence;
  }
  canStart(state, sequence) {
    return this.mode === 'starting' && this.sequence === sequence && answeredOnComputer(state) && state.callSequence === sequence;
  }
  markRunning(sequence) {
    if (this.mode !== 'starting' || this.sequence !== sequence) return false;
    this.mode = 'running'; return true;
  }
  finish(sequence) {
    if (!this.automatic || this.sequence !== sequence || !['starting', 'running', 'finishing'].includes(this.mode)) return false;
    this.mode = 'finishing';
    return true;
  }
  waitNext(sequence) {
    // Only the owner of this completed call may resume after audio cleanup.
    // cancel() or a different sequence makes a late completion harmless.
    if (!this.automatic || this.mode !== 'finishing' || this.sequence !== sequence) return false;
    this.baseline = Math.max(this.baseline ?? 0, sequence);
    this.allowBaseline = false;
    this.sequence = null;
    this.uncertainSince = null;
    this.mode = 'armed';
    return true;
  }
  stop(reason) { this.cancel(); return { type: 'stop', reason }; }
  complete(reason) {
    const sequence = this.sequence;
    if (!this.finish(sequence)) return this.stop(reason);
    return { type: 'stop', reason, resume: true, sequence };
  }
  observe(state, now = Date.now(), { allowStart = true } = {}) {
    if (this.mode === 'idle') return null;
    if (!trustedPhoneState(state)) {
      if (this.mode === 'armed' || this.mode === 'finishing') {
        this.uncertainSince ??= now;
        if (now - this.uncertainSince < 3000) return null;
        this.cancel();
        return { type: 'warning', reason: 'Ожидание отменено: нет свежего состояния телефона. Включите его снова после восстановления связи.' };
      }
      this.uncertainSince ??= now;
      return now - this.uncertainSince >= 3000 ? this.stop('AI остановлен: состояние звонка не подтверждается более трёх секунд.') : null;
    }
    if (this.mode === 'armed') {
      if (state.callPhase === 'unknown') {
        this.uncertainSince ??= now;
        if (now - this.uncertainSince < 3000) return null;
      }
      if (['multiple', 'unknown'].includes(state.callPhase)) {
        this.cancel();
        return { type: 'warning', reason: 'Ожидание выключено: не удалось однозначно определить активный звонок. Включите помощника перед новым звонком.' };
      }
      this.uncertainSince = null;
      const newSequence = state.callSequence > this.baseline || (this.allowBaseline && state.callSequence === this.baseline);
      // Direction does not restrict the assistant; a confirmed answered call does.
      if (allowStart && newSequence && answeredOnComputer(state)) {
        this.mode = 'starting'; this.sequence = state.callSequence;
        return { type: 'start', sequence: state.callSequence };
      }
      return null;
    }
    if (state.callSequence !== this.sequence) {
      if (this.automatic && state.callSequence > this.sequence && ['connected', 'dialing', 'incoming', 'idle', 'ended'].includes(state.callPhase)) {
        if (this.mode === 'finishing') return null;
        return this.complete('Предыдущий разговор завершён; освобождаю аудио для нового звонка.');
      }
      return this.stop('AI остановлен: состояние активного звонка изменилось.');
    }
    if (['ended', 'idle'].includes(state.callPhase)) {
      this.uncertainSince = null;
      if (this.mode === 'finishing') return null;
      return this.complete('Сотовый разговор завершён. AI и аудиомост отключены.');
    }
    if (state.callPhase === 'lost') return this.stop('Связь со звонком потеряна. AI и ожидание следующих звонков отключены.');
    if (state.callPhase === 'unknown') {
      if (this.mode === 'finishing') return this.stop('Ожидание следующих звонков отключено: состояние телефона не определено.');
      this.uncertainSince ??= now;
      return now - this.uncertainSince >= 3000 ? this.stop('AI остановлен: состояние звонка не определено.') : null;
    }
    if (state.callPhase === 'multiple') return this.stop('AI остановлен: обнаружено несколько звонков.');
    if (state.callPhase === 'held') return this.stop('AI остановлен: звонок поставлен на удержание.');
    if (!state.audioOnComputer) return this.stop('Звук переведён на iPhone. AI и аудиомост отключены.');
    if (!state.active || state.callPhase !== 'connected') return this.stop('AI остановлен: разговор больше не подтверждён.');
    this.uncertainSince = null;
    return null;
  }
}
