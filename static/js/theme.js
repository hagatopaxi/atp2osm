// Light or dark, chosen by the reader and kept across pages. Loaded in <head>,
// before the body paints, so a dark reader never sees a white flash.
// The choice lives in localStorage; with none, the browser's preference wins.
(function () {
    const STORAGE_KEY = 'theme';

    function preferred() {
        try {
            const stored = localStorage.getItem(STORAGE_KEY);
            if (stored === 'light' || stored === 'dark') return stored;
        } catch (e) { /* storage blocked: fall through to the browser */ }
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function apply(theme) {
        document.documentElement.dataset.theme = theme;
    }

    apply(preferred());

    document.addEventListener('DOMContentLoaded', function () {
        const toggle = document.getElementById('theme-toggle');
        if (!toggle) return;
        toggle.checked = document.documentElement.dataset.theme === 'dark';
        toggle.addEventListener('change', function () {
            const theme = this.checked ? 'dark' : 'light';
            apply(theme);
            try { localStorage.setItem(STORAGE_KEY, theme); } catch (e) { /* not persisted, still applied */ }
        });
    });
})();
