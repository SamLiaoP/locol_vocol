// Standalone pages use the same saved preference as the Vue workspace.
(() => {
    const buttons = document.querySelectorAll('[data-static-theme-toggle]');
    const sync = () => {
        const dark = document.documentElement.classList.contains('dark');
        for (const button of buttons) {
            button.setAttribute('aria-pressed', String(dark));
            button.setAttribute('aria-label', dark ? '切換為明亮模式' : '切換為深色模式');
            button.title = button.getAttribute('aria-label');
        }
    };
    for (const button of buttons) button.addEventListener('click', () => {
        const dark = document.documentElement.classList.toggle('dark');
        try { localStorage.setItem('darkMode', String(dark)); } catch (_) {}
        sync();
    });
    sync();
})();
