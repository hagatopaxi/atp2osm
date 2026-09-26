const LG_BREAKPOINT = 1024;
const MD_BREAKPOINT = 768;
const STORAGE_KEY = 'sidebar_collapsed';

const toggle = document.getElementById('sidebar-toggle');
const sidebar = document.getElementById('sidebar');

const isMobile = () => window.innerWidth < MD_BREAKPOINT;

toggle.addEventListener('change', function () {
    // On mobile the overlay always starts closed, so nothing to persist.
    if (!isMobile()) localStorage.setItem(STORAGE_KEY, this.checked ? '0' : '1');
});

(function () {
    const stored = localStorage.getItem(STORAGE_KEY);
    const open = isMobile() ? false
        : stored !== null ? stored !== '1' : window.innerWidth >= LG_BREAKPOINT;
    sidebar.style.transition = 'none';
    toggle.checked = open;
    sidebar.offsetWidth;
    sidebar.style.transition = '';
})();

let lastBreakpoint = window.innerWidth >= LG_BREAKPOINT ? 'lg' : 'md';
window.addEventListener('resize', function () {
    const bp = window.innerWidth >= LG_BREAKPOINT ? 'lg' : 'md';
    if (bp !== lastBreakpoint) {
        lastBreakpoint = bp;
        toggle.checked = bp === 'lg';
        localStorage.removeItem(STORAGE_KEY);
    }
});

// A <details> dropdown only closes from its own summary: a click anywhere
// else on the page closes the open ones of the sidebar too.
document.addEventListener('click', function (event) {
    sidebar.querySelectorAll('details[open]').forEach(function (details) {
        if (!details.contains(event.target)) details.removeAttribute('open');
    });
});
