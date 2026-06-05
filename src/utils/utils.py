def get_total_engagement(post_record) -> int:
    """
    Safely calculates the total engagement (likes + reposts + replies)
    from an ATProto post record, handling None attributes gracefully.
    """
    if post_record is None:
        return 0
    likes = getattr(post_record, "like_count", 0) or 0
    reposts = getattr(post_record, "repost_count", 0) or 0
    quotes = getattr(post_record, "quote_count", 0) or 0
    replies = getattr(post_record, "reply_count", 0) or 0
    return int(likes + reposts + quotes + replies)
