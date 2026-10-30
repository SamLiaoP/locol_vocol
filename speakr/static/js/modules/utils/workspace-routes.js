export const WORKSPACE_PAGES = Object.freeze({
    recordings: { path: '/', label: '錄音' },
    people: { path: '/people', label: '人物' },
    account: { path: '/account', label: '設定' },
    references: { path: '/references', label: '參考與致謝' },
    admin: { path: '/admin', label: '管理' },
    groups: { path: '/group-management', label: '群組管理' },
    inquire: { path: '/inquire', label: '查詢' },
});

export function workspacePageFromPath(pathname) {
    const path = (pathname || '/').replace(/\/$/, '') || '/';
    return Object.keys(WORKSPACE_PAGES).find(key => WORKSPACE_PAGES[key].path === path) || 'recordings';
}

export function workspaceFrameUrl(page, hash = '') {
    const entry = WORKSPACE_PAGES[page];
    return entry && page !== 'recordings' ? `${entry.path}?embedded=1${hash}` : null;
}
