from chatwarden import core


def main():
    reply = (
        "结论：公开资料支持两颗都还在，右侧做过修补，不支持整颗切除。[1][2]\n\n"
        "当时发生了什么\n"
        "2002 年舞台事故后接受了清瘀血和修补手术。\n\n"
        "来源\n"
        "https://www.epochtimes.com/gb/2/4/17/n184039.htm\n"
        "http://ent.sina.com.cn/s/h/2002-04-19/80191.html\n"
        "https://news.qq.com/rain/a/20240614A091M000"
    )

    chunks = core.split_agent_reach_reply(reply)
    assert len(chunks) >= 3, (
        "Agent Reach body and raw source URLs must not share one WeChat message"
    )
    assert "结论：" in chunks[0]
    assert "当时发生了什么" not in chunks[0], (
        "URL-bearing research answers must send the conclusion paragraph first"
    )
    assert "http://" not in chunks[0]
    assert "https://" not in chunks[0]
    assert any("https://www.epochtimes.com/" in chunk for chunk in chunks[1:])
    assert any("http://ent.sina.com.cn/" in chunk for chunk in chunks[1:])

    no_sources = "结论：普通调查正文，没有来源区。"
    assert core.split_agent_reach_reply(no_sources) == [no_sources]

    heading_without_url = "结论：正文。\n\n来源\n同期公开报道"
    assert core.split_agent_reach_reply(heading_without_url) == [heading_without_url]

    print("V355_REACH_DELIVERY_ISOLATION=PASS")


if __name__ == "__main__":
    main()
