    // --- Initialize ---
    initDragDrop();
    initWebSocket();
    initExpandedCards();
    loadReviewGateBadges();
    initBoardFilters();
    initNotificationSettings();
    fetchAgentApprovals();
    // Approvals refresh via WebSocket events only (no polling)
