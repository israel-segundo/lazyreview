/* Interactive PR chat rail helpers (Alpine mixin). */
(function () {
  window.LR = window.LR || {};

  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function sanitizeHtml(html) {
    const allowed = new Set([
      'A', 'P', 'BR', 'STRONG', 'EM', 'B', 'I', 'UL', 'OL', 'LI', 'CODE', 'PRE',
      'H1', 'H2', 'H3', 'H4', 'BLOCKQUOTE', 'HR', 'TABLE', 'THEAD', 'TBODY',
      'TR', 'TH', 'TD', 'DIV', 'SPAN', 'SVG', 'PATH', 'G', 'RECT', 'CIRCLE',
      'TEXT', 'LINE', 'POLYLINE', 'POLYGON', 'FOREIGN_OBJECT'
    ]);
    const wrap = document.createElement('div');
    wrap.innerHTML = html;
    wrap.querySelectorAll('*').forEach((el) => {
      if (!allowed.has(el.tagName)) {
        el.replaceWith(...el.childNodes);
        return;
      }
      [...el.attributes].forEach((attr) => {
        const n = attr.name.toLowerCase();
        if (n.startsWith('on') || n === 'srcdoc') el.removeAttribute(attr.name);
        if (n === 'href' || n === 'xlink:href') {
          const v = (attr.value || '').trim();
          if (/^\s*javascript:/i.test(v)) el.removeAttribute(attr.name);
          else if (n === 'href') {
            el.setAttribute('target', '_blank');
            el.setAttribute('rel', 'noopener noreferrer');
          }
        }
      });
    });
    return wrap.innerHTML;
  }

  function enhanceBareTokens(markdown) {
    // Lightly wrap common bare tokens so they render as <code> when the model
    // forgot backticks (skip fenced blocks and existing inline code).
    const parts = String(markdown || '').split(/(```[\s\S]*?```|`[^`\n]+`)/g);
    return parts
      .map((part, i) => {
        if (i % 2 === 1) return part; // already code / fence
        return part
          .replace(/\b([a-f0-9]{7,40})\b/gi, '`$1`')
          .replace(
            /(?<![\w`/])((?:[\w.-]+\/)+[\w.-]+\.[\w.+-]+)\b/g,
            '`$1`'
          )
          .replace(/\b([A-Za-z_][\w]*\[\])\b/g, '`$1`');
      })
      .join('');
  }

  function wrapTables(html) {
    return String(html || '').replace(
      /<table\b[\s\S]*?<\/table>/gi,
      (table) => '<div class="lr-chat-table-wrap">' + table + '</div>'
    );
  }

  window.LR.renderChatMarkdown = function (text) {
    let html = '';
    const raw = enhanceBareTokens(String(text || ''));
    if (window.marked && typeof window.marked.parse === 'function') {
      html = window.marked.parse(raw, { breaks: true, gfm: true });
    } else {
      html = '<p>' + escapeHtml(raw).replace(/\n/g, '<br>') + '</p>';
    }
    html = html.replace(
      /<pre><code class="language-mermaid">([\s\S]*?)<\/code><\/pre>/gi,
      function (_m, code) {
        const decoded = code
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>')
          .replace(/&amp;/g, '&')
          .replace(/&quot;/g, '"');
        return (
          '<div class="lr-chat-mermaid" data-mermaid="' +
          encodeURIComponent(decoded) +
          '"><pre class="text-xs opacity-70">diagram…</pre></div>'
        );
      }
    );
    return sanitizeHtml(wrapTables(html));
  };

  window.LR.hydrateChatMermaid = async function (root) {
    if (!root || !window.mermaid) return;
    const nodes = root.querySelectorAll('.lr-chat-mermaid[data-mermaid]');
    for (const node of nodes) {
      if (node.dataset.rendered) continue;
      const src = decodeURIComponent(node.getAttribute('data-mermaid') || '');
      try {
        const id = 'mmd-' + Math.random().toString(36).slice(2);
        const { svg } = await window.mermaid.render(id, src);
        node.innerHTML = svg;
        node.dataset.rendered = '1';
      } catch (e) {
        node.innerHTML = '<pre class="text-xs text-red-600">' + escapeHtml(String(e)) + '</pre>';
        node.dataset.rendered = '1';
      }
    }
  };

  function formatApiDetail(detail, fallback) {
    if (detail == null || detail === '') return fallback || 'Request failed';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && (d.msg || d.message)) || JSON.stringify(d))
        .filter(Boolean)
        .join('; ') || fallback || 'Request failed';
    }
    if (typeof detail === 'object' && (detail.message || detail.msg)) {
      return detail.message || detail.msg;
    }
    try {
      return JSON.stringify(detail);
    } catch (e) {
      return fallback || 'Request failed';
    }
  }

  window.LR.chatRailMixin = function (opts) {
    opts = opts || {};
    const storageKey = opts.storageKey || 'lazyreview-chat-open';
    const widthKey = (opts.storageKey || 'lazyreview-chat') + '-width';
    // Width is a fraction of the page: default + floor at 40%, ceiling at 60%.
    const CHAT_MIN_FRAC = 0.4;
    const CHAT_DEFAULT_FRAC = 0.4;
    const CHAT_MAX_FRAC = 0.6;
    const ABS_MIN_CHAT_WIDTH = 280; // floor on very narrow viewports

    return {
      chatOpen: false,
      chatWidthPx: null, // resolved against page width on init / open
      chatHeightPx: null,
      chatMode: opts.mode || 'explore',
      chatPrId: opts.prId || null,
      chatConsolidationId: opts.consolidationId || null,
      chatSession: null,
      chatTurns: [],
      chatDraft: '',
      chatBusy: false,
      chatError: '',
      chatAgent: opts.defaultAgent || (window.LR.AgentName && window.LR.AgentName.CLAUDE) || 'claude',
      chatModel: opts.defaultModel || (window.LR.ClaudeModelAlias && window.LR.ClaudeModelAlias.OPUS) || 'opus',
      chatEffort: opts.defaultEffort || (window.LR.EffortLevel && window.LR.EffortLevel.HIGH) || 'high',
      chatModelOptions: [],
      chatEffortOptions: [],
      chatOpenCombo: null,
      chatAgentQuery: '',
      chatModelQuery: '',
      chatEffortQuery: '',
      chatComboHighlight: -1,
      _agentsCatalog: null,
      _chatResizing: false,
      _chatRootEl: null,
      _onChatViewport: null,
      _onChatWindowResize: null,

      async initChatRail() {
        // Prefer the chat row container for width math (40% of that row).
        this._chatRootEl =
          (this.$el && this.$el.querySelector && this.$el.querySelector('.lr-page-with-chat')) ||
          this.$el;

        try {
          const stored = localStorage.getItem(storageKey);
          if (stored === '1') this.chatOpen = true;
          else if (stored === '0') this.chatOpen = false;
          else this.chatOpen = !!opts.defaultOpen;
        } catch (e) {
          this.chatOpen = !!opts.defaultOpen;
        }
        this.syncChatOpenClass();

        // Settings are cheap; agent catalog shells out to claude/cursor/agy and
        // can take seconds — never block opening/restoring the rail on it.
        await this.loadChatDefaultsFromSettings();
        this.refreshChatModelOptions();
        this.chatSyncComboQueries();
        this.loadAgentsCatalog().then(() => {
          this.refreshChatModelOptions();
          this.chatSyncComboQueries();
        });

        this._onChatViewport = () => {
          if (this._chatResizing) return;
          this.syncChatRailHeight();
        };
        this._onChatWindowResize = () => {
          if (this._chatResizing) return;
          this.clampChatWidth();
          this.syncChatRailHeight();
        };
        window.addEventListener('resize', this._onChatWindowResize);
        window.addEventListener('scroll', this._onChatViewport, { passive: true });

        this.$nextTick(() => {
          this.restoreChatWidth();
          requestAnimationFrame(() => this.syncChatRailHeight());
        });
        if (typeof this.$watch === 'function') {
          this.$watch('chatOpen', () => {
            this.syncChatOpenClass();
            this.$nextTick(() => {
              requestAnimationFrame(() => this.syncChatRailHeight());
            });
          });
        }
        if (this.chatOpen) await this.ensureChatSession({ quiet: true });
      },

      syncChatOpenClass() {
        try {
          document.documentElement.classList.toggle('lr-chat-open', !!this.chatOpen);
        } catch (e) {}
        if (this.chatOpen) {
          try {
            window.scrollTo(0, 0);
          } catch (e) {}
        }
      },

      chatPageWidth() {
        const parent = this._chatRootEl || this.$el;
        const w = (parent && parent.clientWidth) || 0;
        return w > 0 ? w : (window.innerWidth || 1200);
      },

      chatMinWidth() {
        return Math.max(ABS_MIN_CHAT_WIDTH, Math.floor(this.chatPageWidth() * CHAT_MIN_FRAC));
      },

      chatDefaultWidth() {
        return Math.max(this.chatMinWidth(), Math.floor(this.chatPageWidth() * CHAT_DEFAULT_FRAC));
      },

      chatMaxWidth() {
        return Math.max(this.chatMinWidth(), Math.floor(this.chatPageWidth() * CHAT_MAX_FRAC));
      },

      restoreChatWidth() {
        let saved = NaN;
        try {
          saved = parseInt(localStorage.getItem(widthKey) || '', 10);
        } catch (e) {}
        const minW = this.chatMinWidth();
        const maxW = this.chatMaxWidth();
        // Drop corrupt / pre-40% widths so we open at the new default.
        if (Number.isFinite(saved) && saved >= minW && saved <= maxW) {
          this.applyChatWidth(saved);
        } else {
          try {
            localStorage.removeItem(widthKey);
          } catch (e) {}
          this.applyChatWidth(this.chatDefaultWidth());
        }
      },

      /** Apply width to Alpine state + the rail DOM (Alpine CSS-vars are unreliable). */
      applyChatWidth(px, opts) {
        opts = opts || {};
        let w = Math.round(Number(px));
        if (!Number.isFinite(w)) w = this.chatDefaultWidth();
        const minW = opts.minW != null ? opts.minW : this.chatMinWidth();
        const maxW = opts.maxW != null ? opts.maxW : this.chatMaxWidth();
        w = Math.min(maxW, Math.max(minW, w));
        this.chatWidthPx = w;
        const rail = this.$refs.chatRail;
        if (rail) {
          const pxW = w + 'px';
          rail.style.width = pxW;
          rail.style.flexBasis = pxW;
          rail.style.flexGrow = '0';
          rail.style.flexShrink = '0';
          rail.style.minWidth = minW + 'px';
          rail.style.maxWidth = 'none';
        }
        return w;
      },

      chatRailStyle() {
        // Width/flex via plain properties (Alpine CSS-vars are unreliable).
        // Height comes from the row: rail stretches to 100% so it can never
        // outgrow the row and clip the composer border (overflow:hidden).
        const minW = this.chatMinWidth();
        const w = Math.max(minW, Number(this.chatWidthPx) || this.chatDefaultWidth());
        const pxW = w + 'px';
        return {
          width: pxW,
          flexBasis: pxW,
          flexGrow: '0',
          flexShrink: '0',
          minWidth: minW + 'px',
          maxWidth: 'none',
        };
      },

      syncChatRailHeight() {
        // Pin the chat row to the remaining space inside .lr-pr-page / viewport.
        // Rail stretches to the row — do not set a taller pixel height on the rail
        // or overflow:hidden clips the composer’s bottom border.
        const row =
          this._chatRootEl ||
          (this.$el && this.$el.querySelector && this.$el.querySelector('.lr-page-with-chat'));
        const rail = this.$refs && this.$refs.chatRail;
        if (!this.chatOpen) {
          this.chatHeightPx = null;
          if (row) {
            row.style.height = '';
            row.style.maxHeight = '';
          }
          if (rail) {
            rail.style.height = '';
            rail.style.maxHeight = '';
            rail.style.removeProperty('--lr-chat-height');
          }
          return;
        }
        if (!row || this._chatResizing) return;
        const top = row.getBoundingClientRect().top;
        let bottom = window.innerHeight - 4;
        const page = row.parentElement;
        if (page) {
          bottom = Math.min(bottom, page.getBoundingClientRect().bottom);
        }
        const h = Math.max(280, Math.floor(bottom - Math.max(0, top)));
        row.style.height = h + 'px';
        row.style.maxHeight = h + 'px';
        this.chatHeightPx = h;
        if (rail) {
          // Match the row’s used box after flex clamping (never taller).
          const used = Math.max(280, Math.round(row.getBoundingClientRect().height) || h);
          this.chatHeightPx = used;
          const pxH = used + 'px';
          rail.style.setProperty('--lr-chat-height', pxH);
          rail.style.height = '100%';
          rail.style.maxHeight = '100%';
        }
      },

      agentKey(name) {
        return (window.LR && window.LR.agentKey)
          ? window.LR.agentKey(name)
          : String(name || '').toLowerCase();
      },

      clampChatWidth() {
        const pageW = this.chatPageWidth();
        if (pageW < 240) return;
        this.applyChatWidth(this.chatWidthPx != null ? this.chatWidthPx : this.chatDefaultWidth());
        try {
          localStorage.setItem(widthKey, String(this.chatWidthPx));
        } catch (e) {}
      },

      persistChatWidth() {
        this.applyChatWidth(this.chatWidthPx != null ? this.chatWidthPx : this.chatDefaultWidth());
        try {
          localStorage.setItem(widthKey, String(this.chatWidthPx));
        } catch (e) {}
      },

      startChatResize(ev) {
        if (!this.chatOpen) return;
        const rail = this.$refs.chatRail;
        if (!rail) return;

        const startX = ev.clientX;
        const rendered = Math.round(rail.getBoundingClientRect().width);
        const minW = this.chatMinWidth();
        const maxW = this.chatMaxWidth();
        const startW = Math.max(minW, rendered || this.chatWidthPx || this.chatDefaultWidth());
        this._chatResizing = true;
        this.applyChatWidth(startW, { minW: minW, maxW: maxW });

        const onMove = (e) => {
          e.preventDefault();
          // Drag left edge leftward => wider chat.
          const next = startW + (startX - e.clientX);
          this.applyChatWidth(next, { minW: minW, maxW: maxW });
        };
        const onUp = () => {
          this._chatResizing = false;
          document.removeEventListener('pointermove', onMove);
          document.removeEventListener('pointerup', onUp);
          document.removeEventListener('pointercancel', onUp);
          document.body.style.cursor = '';
          document.body.style.userSelect = '';
          this.persistChatWidth();
          this.syncChatRailHeight();
        };

        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        document.addEventListener('pointermove', onMove);
        document.addEventListener('pointerup', onUp);
        document.addEventListener('pointercancel', onUp);
      },

      async loadChatDefaultsFromSettings() {
        try {
          const res = await fetch('/api/settings');
          if (!res.ok) return;
          const data = await res.json();
          if (data.session_agent) this.chatAgent = data.session_agent;
          if (data.session_model) this.chatModel = data.session_model;
          if (data.session_effort != null && data.session_effort !== '') {
            this.chatEffort = data.session_effort;
          }
        } catch (e) {}
      },

      persistChatOpen() {
        try {
          localStorage.setItem(storageKey, this.chatOpen ? '1' : '0');
        } catch (e) {}
        this.syncChatOpenClass();
      },

      openChat() {
        this.chatOpen = true;
        this.persistChatOpen();
        this.$nextTick(() => {
          this.clampChatWidth();
          requestAnimationFrame(() => this.syncChatRailHeight());
        });
        this.ensureChatSession({ quiet: true });
      },

      autoSizeChatInput(el) {
        if (!el) return;
        // Grow with content up to CSS max-height; browsers without field-sizing
        // still get a usable default from rows/min-height.
        if (typeof CSS !== 'undefined' && CSS.supports && CSS.supports('field-sizing', 'content')) {
          return;
        }
        el.style.height = 'auto';
        const max = 16 * 16; // 16rem ≈ px at default root
        el.style.height = Math.min(el.scrollHeight, max) + 'px';
      },

      renderChatMarkdown(text) {
        const html = window.LR.renderChatMarkdown(text);
        this.$nextTick(() => {
          window.LR.hydrateChatMermaid(this.$refs.chatTurns);
        });
        return html;
      },

      formatChatTime(raw) {
        if (!raw) return '';
        if (window.LR && typeof window.LR.formatAbsolute === 'function') {
          return window.LR.formatAbsolute(raw) || String(raw);
        }
        return String(raw);
      },

      async loadAgentsCatalog() {
        try {
          const res = await fetch('/api/agents');
          if (!res.ok) return;
          this._agentsCatalog = await res.json();
        } catch (e) {}
      },

      chatAgentsList() {
        const cat = this._agentsCatalog;
        if (cat && Array.isArray(cat.agents) && cat.agents.length) {
          return cat.agents.slice().sort((a, b) =>
            String(a.display_name || a.name).localeCompare(
              String(b.display_name || b.name),
              undefined,
              { sensitivity: 'base' }
            )
          );
        }
        return Object.values(window.LR.AgentName || { CLAUDE: 'claude', CURSOR: 'cursor', AGY: 'agy', CODEX: 'codex' }).map(
          (name) => ({ name: name, display_name: name, available: true })
        );
      },

      refreshChatModelOptions() {
        const cat = this._agentsCatalog;
        const wantModel =
          this.chatModel ||
          (window.LR.ClaudeModelAlias && window.LR.ClaudeModelAlias.OPUS) ||
          'opus';
        const wantEffort =
          this.chatEffort ||
          (window.LR.EffortLevel && window.LR.EffortLevel.HIGH) ||
          'high';
        let models = [];
        let efforts = [];
        if (cat && Array.isArray(cat.agents)) {
          const a = cat.agents.find((x) => x.name === this.chatAgent || x.id === this.chatAgent);
          if (a) {
            models = (a.models || []).slice();
            efforts = a.efforts || a.effort_options || [];
          }
        }
        if (!models.length && this.chatAgent === 'claude') {
          models = Object.values(window.LR.ClaudeModelAlias || {}).map((id) => ({ id, label: id }));
          efforts = Object.values(window.LR.EffortLevel || {});
        }
        models.sort((x, y) =>
          String(x.label || x.id).localeCompare(String(y.label || y.id), undefined, {
            sensitivity: 'base',
          })
        );
        const selected = models.find((m) => (m.id || m) === wantModel);
        if (selected && Array.isArray(selected.efforts) && selected.efforts.length) {
          efforts = selected.efforts;
        }
        if (!efforts.length) {
          efforts = Object.values(window.LR.EffortLevel || {});
        }
        this.chatModelOptions = models;
        this.chatEffortOptions = efforts;
        const ids = models.map((m) => m.id || m);
        this.chatModel = ids.includes(wantModel)
          ? wantModel
          : ids.includes('opus')
            ? 'opus'
            : ids[0] || wantModel;
        this.chatEffort = efforts.includes(wantEffort)
          ? wantEffort
          : efforts.includes('high')
            ? 'high'
            : efforts[0] || wantEffort;
        this.chatSyncComboQueries();
      },

      _chatMatch(haystack, query) {
        const q = String(query || '').trim().toLowerCase();
        if (!q) return true;
        return String(haystack || '').toLowerCase().includes(q);
      },

      chatAgentLabel() {
        const a = this.chatAgentsList().find((x) => x.name === this.chatAgent);
        if (!a) return this.chatAgent || '';
        return (a.display_name || a.name) + (a.available === false ? ' (missing)' : '');
      },

      chatModelLabel() {
        const m = (this.chatModelOptions || []).find((x) => (x.id || x) === this.chatModel);
        return m ? m.label || m.id || m : this.chatModel || '';
      },

      chatEffortLabel(value) {
        const e = String(value != null ? value : this.chatEffort || '').trim();
        if (!e) return '';
        return e.charAt(0).toUpperCase() + e.slice(1);
      },

      chatCloseCombo(which) {
        if (this.chatOpenCombo === which) {
          this.chatOpenCombo = null;
          this.chatComboHighlight = -1;
          this.chatSyncComboQueries();
        }
      },

      chatSyncComboQueries() {
        if (this.chatOpenCombo !== 'agent') this.chatAgentQuery = this.chatAgentLabel();
        if (this.chatOpenCombo !== 'model') this.chatModelQuery = this.chatModelLabel();
        if (this.chatOpenCombo !== 'effort') this.chatEffortQuery = this.chatEffort || '';
      },

      chatFilteredAgents() {
        const q = this.chatOpenCombo === 'agent' ? this.chatAgentQuery : '';
        return this.chatAgentsList().filter(
          (a) => this._chatMatch(a.display_name, q) || this._chatMatch(a.name, q)
        );
      },

      chatFilteredModels() {
        const q = this.chatOpenCombo === 'model' ? this.chatModelQuery : '';
        return (this.chatModelOptions || []).filter(
          (m) => this._chatMatch(m.label || m.id || m, q) || this._chatMatch(m.id, q)
        );
      },

      chatFilteredEfforts() {
        const q = this.chatOpenCombo === 'effort' ? this.chatEffortQuery : '';
        return (this.chatEffortOptions || []).filter((e) => this._chatMatch(e, q));
      },

      chatComboOptions(which) {
        if (which === 'agent') return this.chatFilteredAgents();
        if (which === 'model') return this.chatFilteredModels();
        return this.chatFilteredEfforts();
      },

      chatComboOptionClass(which, idx, disabled) {
        const base = 'lr-chat-combo__option';
        if (disabled) return base + ' lr-chat-combo__option--disabled';
        if (this.chatOpenCombo === which && this.chatComboHighlight === idx) {
          return base + ' lr-chat-combo__option--active';
        }
        return base;
      },

      chatOpenAgentCombo() {
        if (this.chatBusy) return;
        this.chatOpenCombo = 'agent';
        this.chatAgentQuery = '';
        this.chatComboHighlight = 0;
      },
      chatOpenModelCombo() {
        if (this.chatBusy) return;
        this.chatOpenCombo = 'model';
        this.chatModelQuery = '';
        this.chatComboHighlight = 0;
      },
      chatOpenEffortCombo() {
        if (this.chatBusy || !this.chatEffortOptions.length) return;
        this.chatOpenCombo = 'effort';
        this.chatEffortQuery = '';
        this.chatComboHighlight = 0;
      },

      chatComboMove(which, delta) {
        if (this.chatOpenCombo !== which) {
          if (which === 'agent') this.chatOpenAgentCombo();
          else if (which === 'model') this.chatOpenModelCombo();
          else this.chatOpenEffortCombo();
        }
        const opts = this.chatComboOptions(which);
        if (!opts.length) {
          this.chatComboHighlight = -1;
          return;
        }
        let i = this.chatComboHighlight;
        if (i < 0) i = 0;
        else i = (i + delta + opts.length) % opts.length;
        this.chatComboHighlight = i;
      },

      chatComboConfirm(which) {
        const opts = this.chatComboOptions(which);
        const item = opts[this.chatComboHighlight] || opts[0];
        if (!item) return;
        if (which === 'agent') this.chatPickAgent(item);
        else if (which === 'model') this.chatPickModel(item);
        else this.chatPickEffort(item);
      },

      chatPickAgent(a) {
        if (!a || a.available === false) return;
        this.chatAgent = a.name;
        this.chatOpenCombo = null;
        this.chatComboHighlight = -1;
        this.refreshChatModelOptions();
        this.persistChatModel();
        this.chatSyncComboQueries();
      },

      chatPickModel(m) {
        if (!m) return;
        this.chatModel = m.id || m;
        this.chatOpenCombo = null;
        this.chatComboHighlight = -1;
        const efforts =
          m.efforts && m.efforts.length
            ? m.efforts
            : this.chatEffortOptions.length
              ? this.chatEffortOptions
              : Object.values(window.LR.EffortLevel || {});
        this.chatEffortOptions = efforts;
        if (!efforts.includes(this.chatEffort)) {
          this.chatEffort = efforts.includes('high') ? 'high' : efforts[0] || '';
        }
        this.persistChatModel();
        this.chatSyncComboQueries();
      },

      chatPickEffort(e) {
        if (e == null) return;
        this.chatEffort = e;
        this.chatOpenCombo = null;
        this.chatComboHighlight = -1;
        this.persistChatModel();
        this.chatSyncComboQueries();
      },

      async persistChatModel() {
        if (!this.chatSession || this.chatSession.status !== 'open') return;
        try {
          const res = await fetch('/api/sessions/' + this.chatSession.id, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              agent: this.chatAgent,
              model: this.chatModel,
              effort: this.chatEffort || null,
            }),
          });
          if (res.ok) this.chatSession = await res.json();
        } catch (e) {}
      },

      _applyOpenChatSessionToPage() {
        if (typeof this.syncFindingsFromOpenChatSession === 'function') {
          this.syncFindingsFromOpenChatSession();
        }
      },

      async ensureChatSession(opts) {
        opts = opts || {};
        if (this.chatSession && this.chatSession.status === 'open') {
          await this.reloadChatTurns();
          this._applyOpenChatSessionToPage();
          return;
        }
        // Quiet restore (page refresh with rail open) should not show Thinking…
        // — the POST only reattaches an existing session / creates a row.
        const quiet = !!opts.quiet;
        if (!quiet) this.chatBusy = true;
        this.chatError = '';
        try {
          let url;
          if (this.chatMode === 'challenge') {
            url = '/api/consolidations/' + this.chatConsolidationId + '/sessions';
          } else {
            url = '/api/prs/' + this.chatPrId + '/sessions';
          }
          const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              agent: this.chatAgent,
              model: this.chatModel,
              effort: this.chatEffort || null,
            }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            const msg = formatApiDetail(data.detail, 'Failed to start session');
            if (res.status === 404 && msg === 'Not Found') {
              throw new Error(
                'Chat API is unavailable (server needs a restart after updating).'
              );
            }
            throw new Error(msg);
          }
          this.chatSession = data;
          this.chatAgent = data.agent || this.chatAgent;
          this.chatModel = data.model || this.chatModel;
          this.chatEffort = data.effort || this.chatEffort;
          await this.reloadChatTurns();
          this._applyOpenChatSessionToPage();
          this.$nextTick(() => this.syncChatRailHeight());
        } catch (e) {
          if (!quiet) this.chatError = String(e.message || e);
        } finally {
          if (!quiet) this.chatBusy = false;
        }
      },

      async reloadChatTurns() {
        if (!this.chatSession) return;
        const res = await fetch('/api/sessions/' + this.chatSession.id + '/turns');
        if (!res.ok) return;
        const data = await res.json();
        this.chatTurns = data.turns || [];
        this.$nextTick(() => {
          const el = this.$refs.chatTurns;
          if (el) el.scrollTop = el.scrollHeight;
          window.LR.hydrateChatMermaid(el);
        });
      },

      async sendChatMessage() {
        const text = (this.chatDraft || '').trim();
        if (!text) return;
        await this.ensureChatSession();
        if (!this.chatSession) return;
        this.chatBusy = true;
        this.chatError = '';
        this.chatDraft = '';
        // Show the user bubble immediately — the POST waits for the whole agent turn.
        const optimisticId = 'local-' + Date.now();
        this.chatTurns = (this.chatTurns || []).concat([
          {
            id: optimisticId,
            role: 'user',
            text: text,
            created_at: new Date().toISOString(),
          },
        ]);
        this.$nextTick(() => {
          const el = this.$refs.chatTurns;
          if (el) el.scrollTop = el.scrollHeight;
        });
        try {
          const res = await fetch('/api/sessions/' + this.chatSession.id + '/messages', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(formatApiDetail(data.detail, 'Send failed'));
          if (data.session) this.chatSession = data.session;
          await this.reloadChatTurns();
          if (typeof this.onChatFindingsUpdated === 'function' && data.session) {
            this.onChatFindingsUpdated(data.session);
          }
        } catch (e) {
          this.chatError = String(e.message || e);
          this.chatDraft = text;
          this.chatTurns = (this.chatTurns || []).filter((t) => t.id !== optimisticId);
        } finally {
          this.chatBusy = false;
        }
      },

      async finishChat() {
        if (!this.chatSession) return;
        this.chatBusy = true;
        this.chatError = '';
        try {
          const res = await fetch('/api/sessions/' + this.chatSession.id + '/done', {
            method: 'POST',
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(formatApiDetail(data.detail, 'Done failed'));
          if (data.session) this.chatSession = data.session;
          if (typeof this.onChatDone === 'function') this.onChatDone(data);
        } catch (e) {
          this.chatError = String(e.message || e);
        } finally {
          this.chatBusy = false;
        }
      },
    };
  };
})();
