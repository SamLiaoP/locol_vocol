// This document is a workspace content page only when actually framed.
(() => {
    if (window.parent === window) return;
    document.documentElement.classList.add('lv-embedded');
    const send = payload => window.parent.postMessage(payload, window.location.origin);
    const pages = { '/': 'recordings', '/people': 'people', '/account': 'account',
        '/references': 'references', '/admin': 'admin', '/group-management': 'groups', '/inquire': 'inquire' };
    document.addEventListener('click', event => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const anchor = event.target.closest('a[href]');
        if (!anchor || anchor.hasAttribute('download') || (anchor.target && anchor.target !== '_self')) return;
        const url = new URL(anchor.href, window.location.href);
        if (url.origin !== window.location.origin) return;
        // Local tab/hash navigation belongs to this document's page logic.
        if (url.pathname === window.location.pathname && url.hash) return;
        const match = url.pathname.match(/^\/recordings\/(\d+)\/?$/);
        if (match) {
            event.preventDefault();
            send({ type: 'locol:navigate', recordingId: Number(match[1]) });
        } else if (pages[url.pathname]) {
            event.preventDefault();
            send({ type: 'locol:navigate', page: pages[url.pathname], hash: url.hash, upload: url.searchParams.get('upload') === '1' });
        }
    });
    window.addEventListener('storage', event => {
        if (event.key === 'darkMode') document.documentElement.classList.toggle('dark', event.newValue === 'true');
    });
})();
