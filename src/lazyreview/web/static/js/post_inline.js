/** Shared helpers for GitHub inline review post planning (diff-aware). */
(function () {
  window.LR = window.LR || {};

  function findingPath(f) {
    return String((f && (f.path || f.file)) || '').trim();
  }

  function hasInlineAnchor(f) {
    const path = findingPath(f);
    const line = f && f.line;
    return !!(path && line != null && line !== '' && !Number.isNaN(Number(line)));
  }

  LR.countInlinePostPlan = function (findings, diffPaths) {
    const list = Array.isArray(findings) ? findings : [];
    const paths = diffPaths instanceof Set ? diffPaths : new Set(diffPaths || []);
    const useDiff = paths.size > 0;
    let inline = 0;
    let summary = 0;
    let offDiff = 0;
    for (const f of list) {
      if (!hasInlineAnchor(f)) {
        summary += 1;
        continue;
      }
      const path = findingPath(f);
      if (useDiff && !paths.has(path)) {
        offDiff += 1;
        summary += 1;
        continue;
      }
      inline += 1;
    }
    return { inline, summary, offDiff, total: list.length };
  };

  LR.formatInlinePostPlan = function (counts) {
    if (!counts || !counts.total) return '';
    const parts = [];
    if (counts.inline) parts.push(counts.inline + ' inline on the diff');
    if (counts.offDiff) {
      parts.push(
        counts.offDiff + ' anchored off the diff (summary only)'
      );
    }
    const summaryOnly = counts.summary - counts.offDiff;
    if (summaryOnly > 0) {
      parts.push(summaryOnly + ' in the summary body only');
    }
    return parts.length ? ('Will post: ' + parts.join(', ') + '.') : '';
  };

  LR.fetchPrDiffPaths = async function (prId) {
    if (!prId) return [];
    const res = await fetch('/api/prs/' + prId + '/diff-paths');
    if (!res.ok) return [];
    const data = await res.json().catch(function () { return {}; });
    return Array.isArray(data.paths) ? data.paths : [];
  };
})();
