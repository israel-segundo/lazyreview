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

  function snapLine(line, available) {
    const set = new Set(available || []);
    if (set.has(line)) return line;
    if (!set.size) return null;
    let best = null;
    let bestDist = Infinity;
    for (const n of set) {
      const d = Math.abs(n - line);
      if (d < bestDist) {
        bestDist = d;
        best = n;
      }
    }
    return best;
  }

  LR.countInlinePostPlan = function (findings, diffMeta) {
    const list = Array.isArray(findings) ? findings : [];
    const paths = new Set((diffMeta && diffMeta.paths) || diffMeta || []);
    const linesByPath = (diffMeta && diffMeta.commentable_lines) || {};
    const usePaths = paths.size > 0;
    const useLines = linesByPath && Object.keys(linesByPath).length > 0;
    let inline = 0;
    let summary = 0;
    let offDiff = 0;
    let badLine = 0;
    for (const f of list) {
      if (!hasInlineAnchor(f)) {
        summary += 1;
        continue;
      }
      const path = findingPath(f);
      const line = Number(f.line);
      if (usePaths && !paths.has(path)) {
        offDiff += 1;
        summary += 1;
        continue;
      }
      if (useLines) {
        const available = linesByPath[path];
        if (!available || !available.length) {
          offDiff += 1;
          summary += 1;
          continue;
        }
        if (snapLine(line, available) == null) {
          badLine += 1;
          summary += 1;
          continue;
        }
      }
      inline += 1;
    }
    return { inline, summary, offDiff, badLine, total: list.length };
  };

  LR.formatInlinePostPlan = function (counts) {
    if (!counts || !counts.total) return '';
    const parts = [];
    if (counts.inline) parts.push(counts.inline + ' inline on the diff');
    if (counts.offDiff) {
      parts.push(counts.offDiff + ' anchored off the diff (summary only)');
    }
    if (counts.badLine) {
      parts.push(counts.badLine + ' on non-diff lines (summary or nearest line at post)');
    }
    const summaryOnly = counts.summary - counts.offDiff - (counts.badLine || 0);
    if (summaryOnly > 0) {
      parts.push(summaryOnly + ' in the summary body only');
    }
    return parts.length ? ('Will post: ' + parts.join(', ') + '.') : '';
  };

  LR.fetchPrDiffPaths = async function (prId) {
    if (!prId) return { paths: [], commentable_lines: {} };
    const res = await fetch('/api/prs/' + prId + '/diff-paths');
    if (!res.ok) return { paths: [], commentable_lines: {} };
    const data = await res.json().catch(function () { return {}; });
    return {
      paths: Array.isArray(data.paths) ? data.paths : [],
      commentable_lines: data.commentable_lines || {},
    };
  };
})();
