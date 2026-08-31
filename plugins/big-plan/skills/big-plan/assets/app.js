// big-plan client: comment dialog, rail population, drawer toggles,
// reactions, decision cards, task checkboxes, copy-feedback and send-feedback buttons.
(function () {
  const REACTION_EMOJI = ["\u{1F44D}", "\u{1F44E}", "\u{1F914}"]; // 👍 👎 🤔

  const relpath = document.body.dataset.relpath;
  const apiBase = relpath ? `/api/comments/${encodeURIPath(relpath)}` : null;
  const planTitle = (document.querySelector(".page-title") || {}).textContent || "plan";
  let printDetails = [];

  window.addEventListener("beforeprint", () => {
    printDetails = Array.from(document.querySelectorAll("details:not([open])"));
    printDetails.forEach((detail) => { detail.open = true; });
  });
  window.addEventListener("afterprint", () => {
    printDetails.forEach((detail) => { detail.open = false; });
    printDetails = [];
  });
  function encodeURIPath(p) {
    return p.split("/").map(encodeURIComponent).join("/");
  }

  // -- Sidebar toggles --
  // Wide plans keep both rails visible by default; at half-screen width they
  // start tucked away. Once a reviewer chooses a state, retain that choice for
  // this browser session so inline comments can be the only comments they see.
  const tocToggle = document.querySelector(".toc-toggle");
  const commentsToggle = document.querySelector(".comments-toggle");
  const tocRail = document.querySelector(".toc-rail");
  const commentsRail = document.querySelector(".comments-rail");
  const SIDEBAR_BREAKPOINT = 1200;
  const sidebarPrefs = {
    toc: "big-plan.sidebar.toc",
    comments: "big-plan.sidebar.comments",
  };

  function readSidebarPref(name) {
    try {
      const value = sessionStorage.getItem(sidebarPrefs[name]);
      return value === "open" || value === "closed" ? value : null;
    } catch (_) {
      return null;
    }
  }

  function writeSidebarPref(name, value) {
    try { sessionStorage.setItem(sidebarPrefs[name], value); } catch (_) { /* unavailable */ }
  }

  function sidebarIsOpen(name) {
    const pref = readSidebarPref(name);
    return pref ? pref === "open" : !window.matchMedia(`(max-width: ${SIDEBAR_BREAKPOINT}px)`).matches;
  }

  function updateSidebarToggle(name, open) {
    const toggle = name === "toc" ? tocToggle : commentsToggle;
    if (!toggle) return;
    toggle.setAttribute("aria-expanded", String(open));
    const label = open
      ? (name === "toc" ? "Hide table of contents" : "Hide comments panel")
      : (name === "toc" ? "Show table of contents" : "Show comments panel");
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    // The control is a small tab on the rail edge, so the chevron points into
    // the direction the rail will move when clicked.
    toggle.textContent = name === "toc" ? (open ? "‹" : "›") : (open ? "›" : "‹");
  }

  function setSidebarOpen(name, open, persist = true) {
    document.body.classList.toggle(`sidebar-${name}-open`, open);
    document.body.classList.toggle(`sidebar-${name}-closed`, !open);
    updateSidebarToggle(name, open);
    if (persist) writeSidebarPref(name, open ? "open" : "closed");
  }

  function toggleSidebar(name) { setSidebarOpen(name, !sidebarIsOpen(name)); }

  function restoreSidebar(name) {
    const pref = readSidebarPref(name);
    if (pref) {
      setSidebarOpen(name, pref === "open", false);
    } else {
      // Leave the layout to CSS until the reviewer makes a choice, so crossing
      // the half-screen breakpoint automatically tucks both rails away.
      document.body.classList.remove(`sidebar-${name}-open`, `sidebar-${name}-closed`);
      updateSidebarToggle(name, sidebarIsOpen(name));
    }
  }

  restoreSidebar("toc");
  restoreSidebar("comments");
  window.matchMedia(`(max-width: ${SIDEBAR_BREAKPOINT}px)`).addEventListener("change", () => {
    ["toc", "comments"].forEach((name) => {
      if (!readSidebarPref(name)) updateSidebarToggle(name, sidebarIsOpen(name));
    });
  });
  if (tocToggle && tocRail) tocToggle.addEventListener("click", () => toggleSidebar("toc"));
  if (commentsToggle && commentsRail) commentsToggle.addEventListener("click", () => toggleSidebar("comments"));
  document.querySelectorAll(".toc a").forEach((a) => {
    a.addEventListener("click", () => {
      if (tocRail && window.matchMedia(`(max-width: ${SIDEBAR_BREAKPOINT}px)`).matches) {
        setSidebarOpen("toc", false);
      }
    });
  });

  // -- Change highlights (diff since last review) --
  const changesToggle = document.querySelector(".changes-toggle");
  if (changesToggle) {
    changesToggle.addEventListener("click", () => {
      const hidden = document.body.classList.toggle("changes-hidden");
      changesToggle.textContent = hidden ? "Show Changes" : "Hide Changes";
    });
  }
  const changesDismiss = document.querySelector(".changes-dismiss");
  if (changesDismiss && relpath) {
    changesDismiss.addEventListener("click", async () => {
      changesDismiss.disabled = true;
      try {
        const res = await fetch(`/api/snapshot/${encodeURIPath(relpath)}/clear`, {
          method: "POST",
        });
        if (res.ok) {
          window.location.reload();
        } else {
          changesDismiss.disabled = false;
        }
      } catch (e) {
        changesDismiss.disabled = false;
      }
    });
  }

  if (!apiBase) {
    return; // index page; no per-plan wiring needed.
  }

  // -- Dialog --
  const dialog = document.getElementById("add-comment-dialog");
  const anchorLabel = document.getElementById("add-comment-anchor");
  const textInput = document.getElementById("add-comment-text");
  const form = document.getElementById("add-comment-form");
  let currentAnchor = null;
  let currentSpan = null; // pending span-comment {anchor, quote, quoteOccurrence}

  // A small quoted-text preview shown in the dialog for span comments. Created
  // in JS so the server-side dialog template stays untouched.
  const spanPreview = document.createElement("div");
  spanPreview.className = "comment-quote";
  spanPreview.hidden = true;
  if (anchorLabel && anchorLabel.parentElement) {
    anchorLabel.parentElement.insertAdjacentElement("afterend", spanPreview);
  }

  function openCommentDialog(anchor, span) {
    currentAnchor = anchor;
    currentSpan = span || null;
    anchorLabel.textContent = anchor;
    if (currentSpan && currentSpan.quote) {
      const q = currentSpan.quote;
      const t = q.length > 140 ? q.slice(0, 140) + "…" : q;
      spanPreview.textContent =
        "on “" + t + "”" + (currentSpan.trimmed ? " (trimmed to this block)" : "");
      spanPreview.hidden = false;
    } else {
      spanPreview.hidden = true;
      spanPreview.textContent = "";
    }
    textInput.value = "";
    dialog.showModal();
    textInput.focus();
  }

  // Section headings live inside <summary>, so clicking their text toggles the
  // section. The adjacent button is deliberately outside that trigger.
  document.querySelectorAll(".section-comment-button[data-anchor]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openCommentDialog(btn.dataset.anchor);
    });
  });

  // Ctrl/Cmd+Enter inside the textarea submits the form — native textarea
  // Enter would otherwise just insert a newline.
  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      form.requestSubmit(document.getElementById("add-comment-submit"));
    }
  });

  form.addEventListener("submit", async (e) => {
    const submitter = e.submitter;
    if (!submitter || submitter.value !== "submit") {
      return;
    }
    e.preventDefault();
    const text = textInput.value.trim();
    if (!text || !currentAnchor) return;

    const payload = { type: "text", anchor: currentAnchor, text };
    if (currentSpan && currentSpan.quote) {
      payload.quote = currentSpan.quote;
      payload.quoteOccurrence = currentSpan.quoteOccurrence || 0;
    }
    try {
      const res = await fetch(apiBase, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error("save failed: " + res.status);
      dialog.close();
      window.location.reload();
    } catch (err) {
      alert("Could not save comment: " + err.message);
    }
  });

  // -- Reactions --
  async function postReaction(anchor, emoji, btnEl) {
    flashSaving(btnEl);
    try {
      const res = await fetch(apiBase, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "reaction", anchor, emoji }),
      });
      if (!res.ok) throw new Error("save failed: " + res.status);
      window.location.reload();
    } catch (err) {
      alert("Could not save reaction: " + err.message);
    }
  }

  // Tap an existing reaction chip to remove it. No confirmation — symmetric
  // with the one-tap-to-add UX.
  document.querySelectorAll(".reaction-chip[data-id]").forEach((chip) => {
    chip.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = chip.dataset.id;
      if (!id) return;
      try {
        const res = await fetch(`${apiBase}/${encodeURIComponent(id)}/delete`, {
          method: "POST",
        });
        if (!res.ok) throw new Error("delete failed: " + res.status);
        window.location.reload();
      } catch (err) {
        alert("Could not remove reaction: " + err.message);
      }
    });
  });

  // Delete (× button) on text / decision comments. No confirm — symmetric
  // with reaction tap-to-remove. Backed by /delete; reload reflects state.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".comment-delete");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const id = btn.dataset.id;
    try {
      const res = await fetch(`${apiBase}/${encodeURIComponent(id)}/delete`, {
        method: "POST",
      });
      if (!res.ok) throw new Error("delete failed: " + res.status);
      window.location.reload();
    } catch (err) {
      alert("Could not delete: " + err.message);
    }
  });

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".comment-resolve");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const id = btn.dataset.id;
    try {
      const res = await fetch(`${apiBase}/${encodeURIComponent(id)}/resolve`, {
        method: "POST",
      });
      if (!res.ok) throw new Error("resolve failed: " + res.status);
      window.location.reload();
    } catch (err) {
      alert("Could not resolve: " + err.message);
    }
  });

  // -- Replies (threads on a comment) --
  // An inline composer rather than the add-comment dialog: a reply belongs to a
  // comment, not to an anchor, and on a phone a textarea that opens in place
  // beats a modal that hides the thread you are answering.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".comment-reply");
    if (!btn || !apiBase) return;
    e.preventDefault();
    e.stopPropagation();
    const comment = btn.closest(".comment");
    if (!comment) return;
    const existing = comment.querySelector(".reply-form");
    if (existing) {
      existing.querySelector("textarea").focus();
      return;
    }
    comment.appendChild(buildReplyForm(btn.dataset.id));
    comment.querySelector(".reply-form textarea").focus();
  });

  function buildReplyForm(id) {
    const form = document.createElement("form");
    form.className = "reply-form";
    const ta = document.createElement("textarea");
    ta.rows = 3;
    ta.required = true;
    ta.placeholder = "Reply...";
    const actions = document.createElement("div");
    actions.className = "reply-form-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "reply-cancel";
    cancel.textContent = "Cancel";
    const send = document.createElement("button");
    send.type = "submit";
    send.className = "reply-send";
    send.textContent = "Reply";
    actions.append(cancel, send);
    form.append(ta, actions);

    cancel.addEventListener("click", () => form.remove());
    ta.addEventListener("keydown", (ev) => {
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
        ev.preventDefault();
        form.requestSubmit();
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        form.remove();
      }
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const text = ta.value.trim();
      if (!text) return;
      send.disabled = true;
      try {
        const res = await fetch(
          `${apiBase}/${encodeURIComponent(id)}/reply`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text, role: "reviewer", author: "reviewer" }),
          }
        );
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.error || `HTTP ${res.status}`);
        }
        window.location.reload();
      } catch (err) {
        send.disabled = false;
        alert("Could not reply: " + err.message);
      }
    });
    return form;
  }

  // -- Answered-thread navigation --
  // The chip walks the answered threads one tap at a time; on a phone nobody
  // scrolls a long plan hunting for where an answer landed.
  const answersCount = document.querySelector(".answers-count");
  if (answersCount) {
    let cursor = -1;
    answersCount.addEventListener("click", () => {
      // An orphaned comment (its anchor text changed, so the block it was
      // attached to no longer exists) still counts server-side and still shows
      // in the rail, but never renders in .content. Falling back to the rail
      // keeps the chip from counting answers it cannot navigate to.
      const seen = new Set();
      const answered = [];
      const push = (el) => {
        const id = el.dataset.id;
        if (id) {
          if (seen.has(id)) return;
          seen.add(id);
        }
        answered.push(el);
      };
      document
        .querySelectorAll('.content .comment[data-answered="true"]')
        .forEach(push);
      document
        .querySelectorAll('#comments-list .comment[data-answered="true"]')
        .forEach(push);
      if (answered.length === 0) return;
      cursor = (cursor + 1) % answered.length;
      const target = answered[cursor];
      // The rail is a drawer on a phone, so a rail target has to be opened
      // before scrolling to it means anything.
      if (commentsRail && commentsRail.contains(target)) {
        setSidebarOpen("comments", true);
      }
      let parent = target.closest("details");
      while (parent) {
        parent.open = true;
        parent = parent.parentElement && parent.parentElement.closest("details");
      }
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("flash");
      setTimeout(() => target.classList.remove("flash"), 1200);
    });
  }

  // -- Decide cards (auto-save on change, "Other..." escape hatch) --
  document.querySelectorAll(".decide-card").forEach((card) => {
    const inputs = card.querySelectorAll(
      "input[type=radio], input[type=checkbox]"
    );
    const saved = card.querySelector(".decide-saved");
    const otherInputs = card.querySelectorAll(".decide-other-input");

    function readOtherValue(toggle) {
      const row = toggle.closest(".decide-option");
      const otherInput = row && row.querySelector(".decide-other-input");
      return otherInput ? (otherInput.value || "").trim() : "";
    }

    const save = async () => {
      const anchor = card.dataset.anchor;
      const multi = card.dataset.multi === "true";
      const checked = card.querySelectorAll("input[type=radio]:checked, input[type=checkbox]:checked");
      const choices = [];
      for (const input of checked) {
        if (input.value === "__OTHER__") {
          const txt = readOtherValue(input);
          if (txt) choices.push(txt);
        } else {
          choices.push(input.value);
        }
      }
      const question = (card.querySelector(".decide-q") || {}).textContent || "";
      try {
        const res = await fetch(apiBase, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "decision",
            anchor,
            choices,
            question: question.trim(),
            multi,
          }),
        });
        if (!res.ok) throw new Error("save failed: " + res.status);
        if (saved) {
          saved.hidden = false;
          saved.textContent = choices.length === 0 ? "cleared" : "saved ✓";
          setTimeout(() => { saved.hidden = true; }, 1500);
        }
      } catch (err) {
        alert("Could not save decision: " + err.message);
      }
    };

    inputs.forEach((input) => input.addEventListener("change", save));

    // Other text input: save on blur (change) when its toggle is checked.
    // Press Enter to check the Other toggle and save.
    otherInputs.forEach((input) => {
      input.addEventListener("change", () => {
        const toggle = input.closest(".decide-option").querySelector("input[type=radio], input[type=checkbox]");
        if (toggle && toggle.checked) save();
      });
      input.addEventListener("keydown", (e) => {
        // Plain Enter and Ctrl/Cmd+Enter both commit — Ctrl+Enter for parity
        // with the comment dialog.
        if (e.key === "Enter") {
          e.preventDefault();
          const toggle = input.closest(".decide-option").querySelector("input[type=radio], input[type=checkbox]");
          if (toggle) toggle.checked = true;
          if ((input.value || "").trim()) save();
        }
      });
      // Don't propagate clicks on the text field to the surrounding label
      // (which would toggle the radio/checkbox).
      input.addEventListener("click", (e) => e.stopPropagation());
    });

    // Radios have no native "clear" — let the user tap a selected radio to
    // unselect it (which triggers the save handler with no checked inputs).
    if (card.dataset.multi !== "true") {
      inputs.forEach((input) => {
        input.addEventListener("click", () => {
          if (input.dataset.wasChecked === "true") {
            input.checked = false;
            input.dataset.wasChecked = "false";
            save();
          } else {
            inputs.forEach((i) => { i.dataset.wasChecked = "false"; });
            input.dataset.wasChecked = "true";
          }
        });
      });
      inputs.forEach((i) => { i.dataset.wasChecked = i.checked ? "true" : "false"; });
    }
  });

  // -- Task checkboxes: mutate `- [ ]` <-> `- [x]` in the source .md --
  // The marker carries data-md-line so the server can flip the exact line.
  const taskApi = relpath ? `/api/task/${encodeURIPath(relpath)}` : null;
  document.querySelectorAll(".task-marker[data-md-line]").forEach((marker) => {
    marker.setAttribute("role", "button");
    marker.setAttribute("tabindex", "0");
    marker.title = "Toggle status";
    const handler = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!taskApi) return;
      const line = parseInt(marker.dataset.mdLine, 10);
      if (!line) return;
      const currentState = marker.dataset.state === "done" ? "done" : "open";
      const newChecked = currentState !== "done";

      // Optimistic UI update.
      marker.dataset.state = newChecked ? "done" : "open";

      try {
        const res = await fetch(taskApi, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ line, checked: newChecked }),
        });
        if (!res.ok) throw new Error("save failed: " + res.status);
      } catch (err) {
        marker.dataset.state = currentState;
        alert("Could not save: " + err.message);
      }
    };
    marker.addEventListener("click", handler);
    marker.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") handler(e);
    });
  });

  // -- Copy round-trip prompt --
  const copyBtn = document.querySelector(".copy-feedback");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      try {
        const text = await buildFeedbackPrompt();
        await copyText(text);
        flashSaving(copyBtn, "copied ✓");
      } catch (err) {
        alert("Could not copy: " + err.message);
      }
    });
  }

  // -- Send feedback to the session that wrote this plan --
  initSendFeedback();
  function initSendFeedback() {
    const btn = document.querySelector(".send-feedback");
    if (!btn || !relpath) return;
    const submitUrl = `/api/submit/${encodeURIPath(relpath)}`;
    let route = null;

    // The topbar has no room for a destination name, so the label stays short
    // and the destination lives in the tooltip, in the dispatch-mode confirm,
    // and in the flash after the send. Only `dispatch` costs a session that was
    // not already the plan's, so that is the one press that asks first.
    const labelEl = btn.querySelector(".sf-label") || btn;
    const baseLabel = labelEl.textContent;

    function applyRoute(r) {
      route = r;
      labelEl.textContent = baseLabel;
      btn.title =
        r.mode === "dispatch"
          ? `No authoring session is recorded for this plan (${r.reason}). ` +
            "Sending starts a fresh one."
          : `Sends all open feedback to ${r.target} (${r.mode}: ${r.reason}).`;
    }

    // Ships disabled from the server so it cannot be pressed before we know
    // where it goes; resolve the destination into the tooltip, then enable.
    (async () => {
      try {
        const res = await fetch(submitUrl);
        if (!res.ok) throw new Error("no route");
        applyRoute(await res.json());
        btn.disabled = false;
      } catch (err) {
        btn.title = "Could not resolve a target session for this plan.";
      }
    })();

    btn.addEventListener("click", async () => {
      if (btn.disabled || !route) return;
      const openCount = await countOpenComments();
      if (openCount === 0) {
        flashSaving(labelEl, "nothing open");
        return;
      }
      if (
        route.mode === "dispatch" &&
        !confirm(
          "No authoring session is recorded for this plan, so this starts a " +
            "brand new session with the feedback. Continue?"
        )
      ) {
        return;
      }
      btn.disabled = true;
      labelEl.textContent = "sending";
      try {
        const prompt = await buildFeedbackPrompt();
        const res = await fetch(submitUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // The server recomputes the route and 409s if it moved, so a button
          // labelled for the authoring session can never quietly spend a new one.
          body: JSON.stringify({
            prompt,
            expectMode: route.mode,
            expectSessionId: route.sessionId,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 409 && data.route) {
          applyRoute(data.route);
          btn.disabled = false;
          alert(
            "The destination changed since this page loaded, so nothing was " +
              `sent. It now goes to: ${data.route.target || "a new session"} ` +
              `(${data.route.reason}). Press again to confirm.`
          );
          return;
        }
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        labelEl.textContent = baseLabel;
        // Names the destination after the fact, since the label cannot. Clipped
        // so a long session name cannot wrap the topbar for the flash duration.
        const dest =
          data.mode === "dispatch" ? "new session" : String(data.target || "");
        flashSaving(
          labelEl,
          (dest.length > 24 ? dest.slice(0, 23) + "…" : dest) + " ✓"
        );
        btn.classList.add("flash");
        setTimeout(() => btn.classList.remove("flash"), 1200);
      } catch (err) {
        labelEl.textContent = baseLabel;
        btn.classList.add("err");
        alert("Could not send: " + err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  async function countOpenComments() {
    try {
      const res = await fetch(apiBase);
      if (!res.ok) return 0;
      const data = await res.json();
      return (data.comments || []).filter((c) => !c.resolved).length;
    } catch (err) {
      return 0;
    }
  }

  // -- Copy full markdown --
  const mdCopyBtn = document.querySelector(".md-copy");
  if (mdCopyBtn && relpath) {
    mdCopyBtn.addEventListener("click", async () => {
      try {
        const res = await fetch(`/raw/${encodeURIPath(relpath)}`);
        if (!res.ok) throw new Error("could not load markdown");
        await copyText(await res.text());
        flashSaving(mdCopyBtn, "copied ✓");
      } catch (err) {
        alert("Could not copy: " + err.message);
      }
    });
  }

  // -- Mermaid diagrams (isolated: a mermaid failure must never break the
  //    comment/reaction/task wiring below) --
  initMermaid();
  function initMermaid() {
    if (!(window.mermaid && document.querySelector(".mermaid"))) return;
    (async () => {
      try {
        const dark =
          window.matchMedia &&
          window.matchMedia("(prefers-color-scheme: dark)").matches;
        window.mermaid.initialize({
          startOnLoad: false,
          theme: dark ? "dark" : "default",
          securityLevel: "strict",
        });
        const nodes = Array.from(document.querySelectorAll(".mermaid"));
        // Stash the raw source so a diagram that fails to render degrades back
        // to its readable source instead of vanishing.
        nodes.forEach((n) => {
          n.dataset.mmSrc = n.textContent;
        });
        if (typeof window.mermaid.run === "function") {
          await window.mermaid.run({ nodes, suppressErrors: true });
        } else {
          for (let i = 0; i < nodes.length; i++) {
            try {
              const out = await window.mermaid.render("mmd-" + i, nodes[i].dataset.mmSrc);
              nodes[i].innerHTML = out.svg;
            } catch (e) {
              // marked below
            }
          }
        }
        // Any node that produced no SVG failed to parse: restore its source and
        // flag it so it reads as text rather than disappearing.
        nodes.forEach((n) => {
          if (!n.querySelector("svg")) {
            n.textContent = n.dataset.mmSrc;
            n.classList.add("mermaid-error");
          }
        });
      } catch (e) {
        document.querySelectorAll(".mermaid").forEach((n) => {
          if (!n.querySelector("svg")) n.classList.add("mermaid-error");
        });
      }
    })();
  }

  // -- Per-code-block copy buttons --
  initCodeCopy();
  function initCodeCopy() {
    const blocks = document.querySelectorAll(".content pre");
    blocks.forEach((pre) => {
      // Host the button on the .hl wrapper (or the pre itself) so it stays
      // pinned while the pre scrolls horizontally.
      const host = pre.closest(".hl") || pre;
      host.classList.add("code-block");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "code-copy";
      btn.textContent = "copy";
      btn.setAttribute("aria-label", "Copy code");
      btn.addEventListener("click", async () => {
        const code = pre.querySelector("code") || pre;
        try {
          await copyText(code.textContent);
          flashSaving(btn, "copied ✓");
        } catch (err) {
          alert("Could not copy: " + err.message);
        }
      });
      host.appendChild(btn);
    });
  }

  async function buildFeedbackPrompt() {
    const res = await fetch(apiBase);
    if (!res.ok) throw new Error("could not load comments");
    const data = await res.json();
    const open = (data.comments || []).filter((c) => !c.resolved);
    if (open.length === 0) {
      return `Plan: ${planTitle}\nSource: ${relpath}\n\nNo open feedback.\n`;
    }
    // Group by anchor; preserve document order of anchors when possible.
    const order = anchorDocOrder();
    const byAnchor = {};
    for (const c of open) {
      (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c);
    }
    const orderedAnchors = [
      ...order.filter((a) => byAnchor[a]),
      ...Object.keys(byAnchor).filter((a) => !order.includes(a)),
    ];
    const lines = [];
    lines.push(`Plan: ${planTitle}`);
    lines.push(`Source: ${relpath}`);
    lines.push("");
    lines.push("Open feedback to address:");
    lines.push("");
    for (const anchor of orderedAnchors) {
      const label = anchorLabelFor(anchor);
      lines.push(`### ${label}  [#${anchor}]`);
      for (const c of byAnchor[anchor]) {
        lines.push("- " + formatCommentLine(c));
        // The thread travels with its comment so a second round of feedback
        // does not read as a fresh question you have already answered.
        for (const r of Array.isArray(c.replies) ? c.replies : []) {
          const who = (r.role || "agent") === "agent" ? "you" : (r.author || "reviewer");
          lines.push(`  - reply (${who}, ${r.timestamp || ""}): ${r.text || ""}`);
        }
        if (isAnswered(c)) {
          lines.push(
            `  - id ${c.id}: ANSWERED by you, no reviewer reply since. Do not answer again.`
          );
        } else {
          lines.push(`  - id ${c.id}`);
        }
      }
      lines.push("");
    }
    const replyUrl =
      `${window.location.origin}/api/comments/${encodeURIPath(relpath)}/<COMMENT_ID>/reply`;
    lines.push(
      "Triage each comment into exactly one of three outcomes:",
      "",
      "1. It asks for a change to the plan -> edit the plan, then delete that",
      "   comment from the sidecar.",
      "2. It is a question for you -> reply in the thread and LEAVE IT OPEN:",
      `     jq -n --arg t 'your answer' '{text:$t}' | curl -sX POST \\`,
      `       '${replyUrl}' \\`,
      "       -H 'Content-Type: application/json' -d @-",
      "   Never resolve or delete a comment you answered this way. Resolved",
      "   comments stop rendering, so you would be deleting your own answer",
      "   before the reviewer saw it; dismissing it is their press, not yours.",
      "3. It needs a decision only the reviewer can make -> leave it open and",
      "   say so as a reply, so the ask is visible on the page."
    );
    return lines.join("\n");
  }

  function formatCommentLine(c) {
    const ts = c.timestamp || "";
    if (c.type === "reaction") {
      return `(reaction ${ts}) ${c.emoji}`;
    }
    if (c.type === "decision") {
      const choices = (c.choices && c.choices.length ? c.choices : [c.choice]).filter(Boolean);
      const q = c.question ? `${c.question} -> ` : "decision -> ";
      return `(decision ${ts}) ${q}${choices.join(", ")}`;
    }
    if (c.type === "status") {
      const state = c.checked ? "done" : "open";
      const t = c.text ? `: ${c.text}` : "";
      return `(status ${ts}) marked ${state}${t}`;
    }
    const quote = (c.quote || "").trim();
    if (quote) {
      const q = quote.length > 140 ? quote.slice(0, 140) + "…" : quote;
      return `(comment ${ts}) on "${q}": ${c.text || ""}`;
    }
    return `(comment ${ts}) ${c.text || ""}`;
  }

  function anchorDocOrder() {
    // Deduped: an anchor that already has comments carries data-anchor twice --
    // once on the block, once on the .comments-inline wrapper render.py emits
    // after it. Without the Set every commented anchor came out of
    // buildFeedbackPrompt as two identical groups.
    return Array.from(
      new Set(
        Array.from(document.querySelectorAll("[data-anchor]"))
          .map((el) => el.dataset.anchor)
      )
    );
  }

  // -- Rail population --
  async function loadCommentsRail() {
    const list = document.getElementById("comments-list");
    if (!list) return;
    try {
      const res = await fetch(apiBase);
      if (!res.ok) throw new Error("status " + res.status);
      const data = await res.json();
      const open = (data.comments || []).filter((c) => !c.resolved);
      if (open.length === 0) {
        list.innerHTML = '<div class="comments-empty">No open comments.</div>';
        return;
      }
      const byAnchor = {};
      for (const c of open) {
        (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c);
      }
      const parts = [];
      for (const anchor of Object.keys(byAnchor)) {
        const label = anchorLabelFor(anchor);
        parts.push('<div class="anchor-group">');
        parts.push(`<h3><a href="#${escapeAttr(anchor)}">${escapeHtml(label)}</a></h3>`);
        for (const c of byAnchor[anchor]) {
          parts.push(renderRailItem(c));
        }
        parts.push("</div>");
      }
      list.innerHTML = parts.join("");
    } catch (err) {
      list.innerHTML = '<div class="comments-empty">Could not load comments.</div>';
    }
  }

  function anchorLabelFor(anchor) {
    if (anchor.startsWith("b-") || anchor.startsWith("c-")) {
      const el = document.querySelector(`[data-anchor="${cssEscape(anchor)}"]`);
      if (el) {
        const text = el.textContent.replace(/\s+/g, " ").trim();
        return text.length > 60 ? text.slice(0, 60) + "..." : text || "#" + anchor;
      }
      return "#" + anchor;
    }
    if (anchor.startsWith("d-")) {
      const el = document.querySelector(`.decide-card[data-anchor="${cssEscape(anchor)}"] .decide-q`);
      if (el) {
        const text = el.textContent.replace(/\s+/g, " ").trim();
        return "decide: " + (text.length > 50 ? text.slice(0, 50) + "..." : text);
      }
      return "#" + anchor;
    }
    return "#" + anchor;
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return s.replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }

  function deleteBtn(cid) {
    return (
      '<button type="button" class="comment-delete" data-id="' +
      escapeAttr(cid) +
      '" title="Delete this comment" aria-label="Delete">×</button>'
    );
  }

  function resolveBtn(cid) {
    return (
      '<button type="button" class="comment-resolve" data-id="' +
      escapeAttr(cid) +
      '" title="Resolve this comment" aria-label="Resolve">✓</button>'
    );
  }

  function replyBtn(cid) {
    return (
      '<button type="button" class="comment-reply" data-id="' +
      escapeAttr(cid) +
      '" title="Reply in this thread" aria-label="Reply">Reply</button>'
    );
  }

  // Mirrors render.py's is_answered: the last word came from an agent, so the
  // thread is waiting on the reviewer.
  function isAnswered(c) {
    const replies = c.replies;
    if (!Array.isArray(replies) || replies.length === 0) return false;
    return (replies[replies.length - 1].role || "agent") === "agent";
  }

  function repliesHtml(c) {
    const replies = Array.isArray(c.replies) ? c.replies : [];
    if (replies.length === 0) return "";
    const parts = replies.map((r) => {
      const role = (r.role || "agent") === "agent" ? "agent" : "reviewer";
      return (
        '<div class="reply ' + role + '">' +
        '<div class="reply-meta"><span class="reply-who">' +
        escapeHtml(r.author || role) +
        '</span><span class="ts">' + escapeHtml(r.timestamp || "") + "</span></div>" +
        '<div class="reply-body">' + escapeHtml(r.text || "") + "</div></div>"
      );
    });
    return '<div class="replies">' + parts.join("") + "</div>";
  }

  function renderRailItem(c) {
    const ts = escapeHtml(c.timestamp || "");
    const cid = escapeAttr(c.id || "");
    const del = deleteBtn(c.id || "");
    const resolve = resolveBtn(c.id || "");
    const rep = replyBtn(c.id || "");
    const thread = repliesHtml(c);
    const answered = isAnswered(c) ? ' data-answered="true"' : "";
    if (c.type === "reaction") {
      return (
        '<div class="comment reaction" data-id="' + cid + '">' +
        '<span class="reaction-emoji">' + escapeHtml(c.emoji || "?") + "</span> " +
        '<span class="ts">' + ts + "</span>" + del + "</div>"
      );
    }
    if (c.type === "decision") {
      const choices = (c.choices && c.choices.length ? c.choices : [c.choice]).filter(Boolean);
      return (
        '<div class="comment decision" data-id="' + cid + '"' + answered + ">" +
        '<div class="comment-meta"><span class="ts">' + ts + "</span>" +
        rep + resolve + del + "</div>" +
        '<div class="comment-body"><strong>Decided:</strong> ' +
        escapeHtml(choices.join(", ")) + "</div>" + thread + "</div>"
      );
    }
    if (c.type === "status") {
      const state = c.checked ? "done" : "open";
      const t = c.text ? ": " + c.text : "";
      return (
        '<div class="comment status" data-id="' + cid + '">' +
        '<div class="comment-meta"><span class="ts">' + ts + "</span>" + resolve + del + "</div>" +
        '<div class="comment-body">Marked ' + escapeHtml(state) +
        escapeHtml(t) + "</div></div>"
      );
    }
    let quoteHtml = "";
    const quote = (c.quote || "").trim();
    if (quote) {
      const truncated = quote.length > 140 ? quote.slice(0, 140) + "…" : quote;
      quoteHtml = '<div class="comment-quote">' + escapeHtml(truncated) + "</div>";
    }
    return (
      '<div class="comment" data-id="' + cid + '"' + answered + ">" +
      '<div class="comment-meta"><span class="ts">' + ts + "</span>" +
      rep + resolve + del + "</div>" +
      quoteHtml +
      '<div class="comment-body">' + escapeHtml(c.text || "") + "</div>" +
      thread + "</div>"
    );
  }

  async function copyText(text) {
    // The async Clipboard API only exists in secure contexts. This service is
    // served over plain HTTP (Tailscale), so fall back to a temporary textarea
    // + execCommand when navigator.clipboard is unavailable.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      if (!document.execCommand("copy")) {
        throw new Error("copy command was rejected by the browser");
      }
    } finally {
      document.body.removeChild(ta);
    }
  }

  function flashSaving(el, msg) {
    if (!el) return;
    const orig = el.textContent;
    el.classList.add("flash");
    if (msg) el.textContent = msg;
    setTimeout(() => {
      el.classList.remove("flash");
      if (msg) el.textContent = orig;
    }, 1200);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }

  loadCommentsRail();
  initTableSort();

  // ===========================================================================
  // Click-to-sort table columns: clicking a header cell sorts that table's
  // <tbody> rows by the clicked column. Guards keep table cells commentable --
  // a click that is part of a text selection or lands on an existing highlight
  // is left alone. Rows are reordered by moving the existing <tr> nodes, never
  // by rebuilding innerHTML, so <mark> span highlights and data-anchors travel
  // with their cells.
  // ===========================================================================
  function initTableSort() {
    document.querySelectorAll(".content table").forEach((table) => {
      const headRow = table.querySelector("thead tr");
      const body = table.querySelector(":scope > tbody");
      if (!headRow || !body) return;
      // Stamp the default rendered order once so a third click can restore it.
      Array.from(body.children)
        .filter((r) => r.tagName === "TR")
        .forEach((row, i) => {
          row.dataset.sortOrig = String(i);
        });
      const headers = headRow.querySelectorAll("th");
      headers.forEach((th, colIndex) => {
        th.addEventListener("click", (e) => {
          // Text-selection-for-comment: never sort while a selection is live.
          const sel = window.getSelection();
          if (sel && !sel.isCollapsed) return;
          // Click on an existing highlight opens its popover, not a sort.
          if (e.target.closest("mark.comment-highlight")) return;
          sortTableByColumn(table, body, headers, th, colIndex);
        });
      });
    });
  }

  function sortTableByColumn(table, body, headers, th, colIndex) {
    // Three-state cycle on the same column: asc -> desc -> none (original
    // order). A different column resets to ascending. State lives on the table
    // dataset. The caret is always cleared from every header first.
    let dir = "asc";
    if (table.dataset.sortCol === String(colIndex)) {
      if (table.dataset.sortDir === "asc") dir = "desc";
      else if (table.dataset.sortDir === "desc") dir = "none";
    }
    headers.forEach((h) => h.classList.remove("sort-asc", "sort-desc"));

    const rows = Array.from(body.children).filter((r) => r.tagName === "TR");

    if (dir === "none") {
      // Restore the default rendered order captured in initTableSort. Move the
      // existing <tr> nodes so <mark> highlights and data-anchors survive.
      rows.sort((ra, rb) => Number(ra.dataset.sortOrig) - Number(rb.dataset.sortOrig));
      rows.forEach((row) => body.appendChild(row));
      table.dataset.sortCol = "";
      table.dataset.sortDir = "";
      return;
    }

    table.dataset.sortCol = String(colIndex);
    table.dataset.sortDir = dir;

    // Direction indicator is a CSS ::after caret via a class on the active th.
    // No text node is injected, so header-cell character offsets stay intact
    // and any span comment anchored in the header is not corrupted.
    th.classList.add(dir === "asc" ? "sort-asc" : "sort-desc");

    const valueOf = (row) => {
      const cell = row.children[colIndex];
      return cell ? cell.textContent.trim() : "";
    };
    const asNumber = (s) => {
      const cleaned = s.replace(/[$,%\s]/g, "");
      if (cleaned === "") return null;
      const n = Number(cleaned);
      return Number.isFinite(n) ? n : null;
    };

    rows.sort((ra, rb) => {
      const a = valueOf(ra);
      const b = valueOf(rb);
      // Empty strings always sort to the bottom, in either direction.
      if (a === "" && b === "") return 0;
      if (a === "") return 1;
      if (b === "") return -1;
      const na = asNumber(a);
      const nb = asNumber(b);
      const cmp =
        na !== null && nb !== null
          ? na - nb
          : a.localeCompare(b, undefined, { sensitivity: "base" });
      return dir === "asc" ? cmp : -cmp;
    });
    rows.forEach((row) => body.appendChild(row));
  }

  // ===========================================================================
  // Span comments (Notion-style): select arbitrary text inside a block and
  // comment on just that selection. A span comment is a normal text comment
  // carrying `quote` + `quoteOccurrence`, so it flows through every existing
  // rail/inline/delete path unchanged; only the extra highlight + selection UI
  // lives here.
  // ===========================================================================

  const SPAN_SKIP = ["comments-inline", "reactions-row", "anchor-actions"];

  // The nearest commentable block for a node: an ancestor [data-anchor] that is
  // not one of the UI wrappers.
  function spanBlockFor(node) {
    let el = node && node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    while (el && el !== document.body) {
      if (el.hasAttribute && el.hasAttribute("data-anchor")) {
        if (
          el.classList.contains("comments-inline") ||
          el.classList.contains("reactions-row") ||
          el.classList.contains("anchor-actions") ||
          el.classList.contains("decide-card")
        ) {
          return null;
        }
        return el;
      }
      el = el.parentElement;
    }
    return null;
  }

  // True if a node lives inside an injected UI wrapper (or script/style), so
  // the text-node walkers can skip it and align with the server-side textContent.
  function isSpanSkippable(node) {
    let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    while (el && el !== document.body) {
      if (el.classList) {
        for (const s of SPAN_SKIP) if (el.classList.contains(s)) return true;
      }
      const tn = el.tagName;
      if (tn === "SCRIPT" || tn === "STYLE") return true;
      el = el.parentElement;
    }
    return false;
  }

  // Ordered text nodes of a block, skipping the UI wrappers. Existing highlight
  // marks are NOT skipped: a <mark> only wraps text, so its characters remain
  // part of the block content and the character basis stays identical on every
  // load regardless of which highlights are present.
  function spanTextNodes(block) {
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return isSpanSkippable(node)
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    return nodes;
  }

  function countOccurrences(haystack, needle) {
    if (!needle) return 0;
    let count = 0;
    let idx = 0;
    while ((idx = haystack.indexOf(needle, idx)) !== -1) {
      count++;
      idx += needle.length;
    }
    return count;
  }

  // -- Floating toolbar shown above an active selection: Comment + reactions --
  const selBar = document.createElement("div");
  selBar.className = "selection-toolbar";

  const commentBtn = document.createElement("button");
  commentBtn.type = "button";
  commentBtn.className = "sel-comment";
  commentBtn.textContent = "Comment";
  selBar.appendChild(commentBtn);

  for (const emoji of REACTION_EMOJI) {
    const rbtn = document.createElement("button");
    rbtn.type = "button";
    rbtn.className = "sel-react";
    rbtn.dataset.emoji = emoji;
    rbtn.title = `React ${emoji}`;
    rbtn.textContent = emoji;
    selBar.appendChild(rbtn);
  }
  document.body.appendChild(selBar);
  let pendingSpan = null;
  // Touch devices get below-selection positioning (to dodge the native iOS
  // Copy/Look Up callout) and scroll-survival during handle drags.
  const isTouch = ("ontouchstart" in window) || (navigator.maxTouchPoints > 0);

  function hideSelBtn() {
    selBar.classList.remove("show");
    pendingSpan = null;
  }

  function updateSelectionButton() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      hideSelBtn();
      return;
    }
    const range = sel.getRangeAt(0);
    // The selection must START inside a commentable block. On a phone, dragging
    // the handles routinely overshoots into a later block; rather than letting
    // commonAncestorContainer resolve above every block (which silently killed
    // the toolbar), we anchor on the start block and clamp the end to it.
    const startBlock = spanBlockFor(range.startContainer);
    if (!startBlock) {
      hideSelBtn();
      return;
    }
    const endBlock = spanBlockFor(range.endContainer);
    const trimmed = endBlock !== startBlock;

    // Effective range: the live selection when it stays inside one block, else
    // clamped from the real start to the END of the start block. The sidecar
    // model + highlightSpan are single-block, so a cross-block drag converges on
    // the intersection with the first block instead of vanishing.
    let effRange = range;
    if (trimmed) {
      const nodes = spanTextNodes(startBlock);
      if (!nodes.length) {
        hideSelBtn();
        return;
      }
      const lastNode = nodes[nodes.length - 1];
      effRange = document.createRange();
      effRange.setStart(range.startContainer, range.startOffset);
      effRange.setEnd(lastNode, lastNode.nodeValue.length);
    }

    const quote = effRange.toString().trim();
    if (!quote) {
      hideSelBtn();
      return;
    }
    // Character offset of the selection start within the block plain text. UI
    // wrappers are appended at the block end, so a start->prefix range never
    // contains their text, matching spanTextNodes' basis. The clamp only moves
    // the END, so the prefix (and thus the occurrence index) is unchanged and
    // stays consistent with highlightSpan's single-block mapping.
    const prefix = document.createRange();
    prefix.setStart(startBlock, 0);
    prefix.setEnd(range.startContainer, range.startOffset);
    const occurrence = countOccurrences(prefix.toString(), quote);
    pendingSpan = { anchor: startBlock.dataset.anchor, quote, quoteOccurrence: occurrence, trimmed };

    selBar.classList.add("show");
    const rect = effRange.getBoundingClientRect();
    const btnW = selBar.offsetWidth || 80;
    const barH = selBar.offsetHeight;
    let left = Math.max(6, Math.min(rect.left, window.innerWidth - btnW - 6));
    let top;
    if (isTouch) {
      // Float just below the selection, close to the text. The iOS callout sits
      // above the selection, so below-placement stays clear of it without
      // parking the toolbar far away at the bottom of the screen.
      top = rect.bottom + 8;
      if (top + barH > window.innerHeight - 6) top = rect.top - barH - 8;
    } else {
      top = rect.top - barH - 6;
      if (top < 6) top = rect.bottom + 6;
    }
    selBar.style.left = left + "px";
    selBar.style.top = top + "px";
  }

  let selDebounce = null;
  document.addEventListener("selectionchange", () => {
    clearTimeout(selDebounce);
    selDebounce = setTimeout(updateSelectionButton, 150);
  });
  const contentEl = document.querySelector(".content");
  if (contentEl) {
    contentEl.addEventListener("mouseup", () => setTimeout(updateSelectionButton, 0));
  }
  let scrollRaf = null;
  window.addEventListener(
    "scroll",
    () => {
      if (!selBar.classList.contains("show")) return;
      const sel = window.getSelection();
      // A live selection (iOS auto-scrolls while dragging the handles near the
      // viewport edge): reposition the toolbar instead of killing it mid-gesture.
      // Only hide once the selection has actually collapsed.
      if (sel && !sel.isCollapsed && pendingSpan) {
        if (scrollRaf) return;
        scrollRaf = requestAnimationFrame(() => {
          scrollRaf = null;
          updateSelectionButton();
        });
        return;
      }
      hideSelBtn();
    },
    true
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideSelBtn();
  });

  // mousedown-preventDefault on the whole toolbar keeps the selection alive
  // through a click on any of its buttons.
  selBar.addEventListener("mousedown", (e) => e.preventDefault());
  commentBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!pendingSpan) return;
    const span = pendingSpan;
    hideSelBtn();
    openCommentDialog(span.anchor, span);
  });
  selBar.querySelectorAll(".sel-react").forEach((rbtn) => {
    rbtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!pendingSpan) return;
      const anchor = pendingSpan.anchor;
      hideSelBtn();
      postReaction(anchor, rbtn.dataset.emoji, rbtn);
    });
  });

  // -- Re-highlight saved span comments on load + make them clickable --
  applySpanHighlights();

  async function applySpanHighlights() {
    let data;
    try {
      const res = await fetch(apiBase);
      if (!res.ok) return;
      data = await res.json();
    } catch (e) {
      return;
    }
    const spans = (data.comments || []).filter(
      (c) =>
        !c.resolved &&
        (c.type === "text" || !c.type) &&
        typeof c.quote === "string" &&
        c.quote.trim()
    );
    for (const c of spans) {
      try {
        highlightSpan(c);
      } catch (e) {
        // Quote no longer present (text edited) or DOM shape changed: skip.
      }
    }
  }

  function locateInMap(map, pos) {
    for (const entry of map) {
      const end = entry.start + entry.node.nodeValue.length;
      if (pos <= end) return { node: entry.node, offset: pos - entry.start };
    }
    if (map.length) {
      const last = map[map.length - 1];
      return { node: last.node, offset: last.node.nodeValue.length };
    }
    return null;
  }

  function highlightSpan(c) {
    const quote = c.quote.trim();
    if (!quote) return;
    const occ =
      Number.isInteger(c.quoteOccurrence) && c.quoteOccurrence >= 0
        ? c.quoteOccurrence
        : 0;
    const block = document.querySelector(`[data-anchor="${cssEscape(c.anchor)}"]`);
    if (!block) return;

    const nodes = spanTextNodes(block);
    let full = "";
    const map = [];
    for (const node of nodes) {
      map.push({ node, start: full.length });
      full += node.nodeValue;
    }
    // Find the occ-th (0-based) non-overlapping occurrence.
    let idx = -1;
    let from = 0;
    let seen = 0;
    while ((idx = full.indexOf(quote, from)) !== -1) {
      if (seen === occ) break;
      seen++;
      from = idx + quote.length;
    }
    if (idx === -1 || seen !== occ) return; // not found -> skip silently

    const startLoc = locateInMap(map, idx);
    const endLoc = locateInMap(map, idx + quote.length);
    if (!startLoc || !endLoc) return;

    const range = document.createRange();
    range.setStart(startLoc.node, startLoc.offset);
    range.setEnd(endLoc.node, endLoc.offset);
    const mark = document.createElement("mark");
    mark.className = "comment-highlight";
    mark.dataset.commentId = c.id || "";
    try {
      range.surroundContents(mark);
    } catch (e) {
      // Range crosses element boundaries: fall back to extract + reinsert.
      const frag = range.extractContents();
      mark.appendChild(frag);
      range.insertNode(mark);
    }
  }

  // -- Popover when a highlight is clicked --
  let openPopover = null;
  function closePopover() {
    if (openPopover) {
      openPopover.remove();
      openPopover = null;
    }
  }

  document.addEventListener("click", (e) => {
    const mark = e.target.closest("mark.comment-highlight");
    if (mark) {
      e.preventDefault();
      e.stopPropagation();
      showMarkPopover(mark);
      return;
    }
    if (openPopover && !e.target.closest(".comment-popover")) closePopover();
  });

  function showMarkPopover(mark) {
    closePopover();
    const id = mark.dataset.commentId || "";
    let text = "";
    const inlineBody = document.querySelector(
      `.comment[data-id="${cssEscape(id)}"] .comment-body`
    );
    if (inlineBody) text = inlineBody.textContent;

    const pop = document.createElement("div");
    pop.className = "comment-popover";
    const body = document.createElement("div");
    body.className = "comment-popover-body";
    body.textContent = text || "(comment)";
    pop.appendChild(body);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "comment-popover-delete";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!id) return;
      try {
        const res = await fetch(`${apiBase}/${encodeURIComponent(id)}/delete`, {
          method: "POST",
        });
        if (!res.ok) throw new Error("delete failed: " + res.status);
        window.location.reload();
      } catch (err) {
        alert("Could not delete: " + err.message);
      }
    });
    pop.appendChild(delBtn);

    const resolveBtn = document.createElement("button");
    resolveBtn.type = "button";
    resolveBtn.className = "comment-popover-resolve";
    resolveBtn.textContent = "Resolve";
    resolveBtn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!id) return;
      try {
        const res = await fetch(`${apiBase}/${encodeURIComponent(id)}/resolve`, {
          method: "POST",
        });
        if (!res.ok) throw new Error("resolve failed: " + res.status);
        window.location.reload();
      } catch (err) {
        alert("Could not resolve: " + err.message);
      }
    });
    pop.appendChild(resolveBtn);

    document.body.appendChild(pop);
    const rect = mark.getBoundingClientRect();
    const popW = pop.offsetWidth;
    const popH = pop.offsetHeight;
    let left = Math.max(6, Math.min(rect.left, window.innerWidth - popW - 6));
    let top = rect.bottom + 6;
    if (top + popH > window.innerHeight - 6) top = rect.top - popH - 6;
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    openPopover = pop;
  }

  // -- KeyboardNav: Linear-style block focus + shortcuts --
  // Focus walks through anchored content blocks (headings, list items,
  // paragraphs, blockquotes, decide cards). The focused block gets a left-
  // border highlight and is scrolled into view; while focused, action keys
  // (c, 1/2/3, Space) operate on it without needing the mouse.

  const KB = (() => {
    let focusedEl = null;

    function navigables() {
      const all = Array.from(document.querySelectorAll("[data-anchor]"));
      return all.filter((el) => {
        if (el.classList.contains("comments-inline")) return false;
        if (el.classList.contains("reactions-row")) return false;
        if (el.classList.contains("anchor-actions")) return false;
        // Skip anything inside a closed <details>, except the H2 in its summary.
        let p = el.parentElement;
        while (p && p !== document.body) {
          if (p.tagName === "DETAILS" && !p.open) {
            const sum = p.querySelector(":scope > summary");
            if (!sum || !sum.contains(el)) return false;
            break;
          }
          p = p.parentElement;
        }
        return true;
      });
    }

    function elInViewport(el) {
      const r = el.getBoundingClientRect();
      return r.top < window.innerHeight && r.bottom > 0;
    }

    function setFocus(el) {
      if (focusedEl) focusedEl.classList.remove("kb-focused");
      focusedEl = el || null;
      if (focusedEl) {
        focusedEl.classList.add("kb-focused");
        focusedEl.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }

    function sectionList() {
      // H2 only — sections are the section boundaries the renderer uses for
      // <details>. Walking smaller blocks (paragraphs, list items, reactions)
      // is too granular; it lands on every embedded thing.
      return navigables().filter((el) => el.tagName === "H2");
    }

    function move(direction) {
      const list = sectionList();
      if (list.length === 0) return;
      if (!focusedEl || !list.includes(focusedEl)) {
        setFocus(list.find(elInViewport) || list[0]);
        return;
      }
      const idx = list.indexOf(focusedEl);
      const next = list[Math.max(0, Math.min(list.length - 1, idx + direction))];
      setFocus(next);
    }

    function containingDetails(el) {
      let p = el && el.parentElement;
      while (p && p !== document.body) {
        if (p.tagName === "DETAILS") return p;
        p = p.parentElement;
      }
      return null;
    }

    function openSection() {
      const d = containingDetails(focusedEl);
      if (d) d.open = true;
    }
    function closeSection() {
      const d = containingDetails(focusedEl);
      if (d) d.open = false;
    }

    function clear() {
      if (focusedEl) focusedEl.classList.remove("kb-focused");
      focusedEl = null;
    }

    function focusedAnchor() {
      return focusedEl ? focusedEl.dataset.anchor : null;
    }

    return { move, openSection, closeSection, clear, setFocus, focusedAnchor, get el() { return focusedEl; } };
  })();

  // -- Help overlay --
  const helpDialog = document.getElementById("help-dialog");
  function toggleHelp() {
    if (!helpDialog) return;
    if (helpDialog.open) helpDialog.close();
    else helpDialog.showModal();
  }

  // -- Sidebar helpers (work on desktop too via these shortcuts) --
  function toggleTocRail() { if (tocRail) toggleSidebar("toc"); }
  function toggleCommentsRail() { if (commentsRail) toggleSidebar("comments"); }

  // -- Global keydown router --
  // Ignore keystrokes when the user is typing in a form field, EXCEPT for
  // Ctrl/Cmd+Enter (form submit) and Escape (close), which form fields need.
  function isEditingTarget(t) {
    if (!t) return false;
    const tag = t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (t.isContentEditable) return true;
    return false;
  }

  document.addEventListener("keydown", (e) => {
    // Form-field-safe keys (always handled even when editing).
    if (e.key === "Escape") {
      // Cascade: native <dialog> closes itself, but explicitly handle sidebars
      // and block focus after that.
      if (helpDialog && helpDialog.open) { helpDialog.close(); return; }
      if (dialog && dialog.open) return; // native dialog handles Escape
      if (tocRail && sidebarIsOpen("toc")) { setSidebarOpen("toc", false); return; }
      if (commentsRail && sidebarIsOpen("comments")) { setSidebarOpen("comments", false); return; }
      if (KB.el) { KB.clear(); return; }
      return;
    }

    if (isEditingTarget(e.target)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return; // leave plain modifier combos alone

    switch (e.key) {
      case "?":
        e.preventDefault();
        toggleHelp();
        return;
      case "j":
      case "ArrowDown":
        e.preventDefault();
        KB.move(1);
        return;
      case "k":
      case "ArrowUp":
        e.preventDefault();
        KB.move(-1);
        return;
      case "o":
        e.preventDefault();
        KB.openSection();
        return;
      case "O":
        e.preventDefault();
        KB.closeSection();
        return;
      case "t":
        e.preventDefault();
        toggleTocRail();
        return;
      case "m":
        e.preventDefault();
        toggleCommentsRail();
        return;
      case "c": {
        const anchor = KB.focusedAnchor();
        if (!anchor) return;
        e.preventDefault();
        openCommentDialog(anchor);
        return;
      }
      case "1":
      case "2":
      case "3": {
        const anchor = KB.focusedAnchor();
        if (!anchor) return;
        const emoji = REACTION_EMOJI[parseInt(e.key, 10) - 1];
        if (!emoji) return;
        e.preventDefault();
        postReaction(anchor, emoji, KB.el);
        return;
      }
      case " ": {
        // Space toggles a task checkbox inside the focused block.
        const el = KB.el;
        if (!el) return;
        const marker = el.querySelector(":scope > .task-marker[data-md-line], :scope .task-marker[data-md-line]");
        if (!marker) return;
        e.preventDefault();
        marker.click();
        return;
      }
    }
  });
})();
