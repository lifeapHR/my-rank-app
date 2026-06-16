import streamlit as st
import re

from utils import (
    safe_generate_content,
    read_files,
    get_url_text,
    get_section,
)


def show(agent_name):
    st.title("書類作成後: 審査＆推薦文作成")
    m_mode = st.radio("分析モード", ["1. 簡易マッチング", "2. 詳細マッチング"], horizontal=True)

    if m_mode == "1. 簡易マッチング":
        col1, col2 = st.columns(2)
        with col1:
            m_age = st.number_input("年齢", 18, 85, 25, key="m_age_3")
            m_ind = st.text_input("応募業種", key="m_ind_3")
        with col2:
            m_job = st.text_input("応募職種", key="m_job_3")

        if st.button("簡易マッチ分析を実行"):
            if not m_ind or not m_job:
                st.warning("業種と職種を入力してください。")
            else:
                prompt = f"""
あなたは採用のプロです。以下の条件から採用マッチ度（％）と、その理由を客観的に出力してください。
条件：年齢{m_age}歳、応募業種：{m_ind}、応募職種：{m_job}
出力フォーマット：
【マッチ度】
(0-100の数字)
【理由】
(簡潔に)
"""
                try:
                    resp = safe_generate_content(prompt)
                    st.markdown(f"<div class='cyber-panel'>{resp.text}</div>", unsafe_allow_html=True)
                except Exception as e:
                    st.error(f"APIエラー: {e}")

    else:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("企業要件")
            c_url_3 = st.text_input("求人URL", key="c_url_3")
            c_info = st.text_area("求人内容", height=130)
            c_files = st.file_uploader("資料", accept_multiple_files=True, key="c_up_3")
        with col2:
            st.subheader("完成書類")
            s_info = st.text_area("追加補足", height=200)
            s_files = st.file_uploader("完成書類", accept_multiple_files=True, key="s_up_3")

        if st.button("詳細審査 & 推薦文作成", type="primary"):
            if not agent_name:
                st.error("アドバイザー名を入力してください。")
            else:
                with st.spinner("審査中..."):
                    c_data = read_files(c_files) + "\n" + (get_url_text(c_url_3) if c_url_3 else "")
                    s_data = read_files(s_files)

                    prompt = f"""
あなたは凄腕ヘッドハンター兼採用担当者です。
企業要件と求職者の書類を照らし合わせ、マッチ度を％で算出し、推薦メールを作成してください。

企業情報：{c_info}\n{c_data}
求職者書類：{s_info}\n{s_data}

---
【絶対ルール】
出力は必ず以下の4つの【】見出しを含め、指定の構成で出力してください。他の【】見出しは作らないでください。

【マッチ度】
(0〜100の数字のみ)

【書類修正アドバイス】
(さらに通過率を上げるための具体的な修正点)

【面接対策】
(想定質問と回答の方向性)

【推薦文】
(以下の構成で作成)
(企業名) 採用ご担当者様
お世話になっております。キャリアアドバイザーの株式会社ライフアップの{agent_name}です。
この度、○○様を推薦させていただきたく、ご連絡申し上げました。

■推薦理由
・(応募企業に活かせる強み)
・(貢献できる理由)
・(懸念点払拭があれば)
(人柄や熱意も含めて200-300字程度、AI記号「」などは禁止)

ぜひ一度、面接にてご本人とお話しいただけますと幸いです。
何卒ご検討のほど、よろしくお願い申し上げます。
"""
                    try:
                        resp = safe_generate_content(prompt)
                        res_m = resp.text
                        match_score_raw = get_section('マッチ度', res_m)
                        ms = int(re.search(r'\d+', match_score_raw).group()) if re.search(r'\d+', match_score_raw) else 0
                        st.metric("最終マッチ度", f"{ms} %")
                        st.markdown(f"#### アドバイス\n<div class='fb-box'>{get_section('書類修正アドバイス', res_m)}</div>", unsafe_allow_html=True)
                        if ms >= 80:
                            st.success("合格ライン突破！")
                            st.code(get_section('推薦文', res_m), language="text")
                        else:
                            st.warning("マッチ度が基準(80%)を下回っています。")
                            st.code(get_section('推薦文', res_m), language="text")
                        st.subheader("面接対策")
                        st.write(get_section('面接対策', res_m))
                    except Exception as e:
                        st.error(f"エラー: {e}")
