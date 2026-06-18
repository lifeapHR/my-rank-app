import streamlit as st


def show():
    st.title("応募時簡易分析: ランク判定")
    col1, col2, col3 = st.columns(3)
    with col1:
        age = st.number_input("年齢", 18, 85, 25)
    with col2:
        job_changes = st.number_input("転職回数", 0, 15, 1)
    with col3:
        short_term = st.number_input("短期離職数", 0, 10, 0)

    if st.button("ランクを判定する", type="primary"):
        if age < 20:
            age_s = -8
        elif 20 <= age <= 21:
            age_s = 8
        elif 22 <= age <= 25:
            age_s = 10
        elif 26 <= age <= 29:
            age_s = 8
        elif 30 <= age <= 35:
            age_s = 7
        else:
            age_s = 5

        job_bonus = 0
        if age <= 24 and job_changes == 0:
            job_bonus = 10
        elif 25 <= age <= 29 and job_changes <= 1:
            job_bonus = 10
        elif 25 <= age <= 29 and job_changes <= 2:
            job_bonus = 7
        elif 30 <= age <= 35 and job_changes <= 2:
            job_bonus = 10
        elif 30 <= age <= 35 and job_changes <= 3:
            job_bonus = 7
        elif 35 <= age <= 85 and job_changes <= 2:
            job_bonus = 10
        elif 35 <= age <= 85 and job_changes <= 3:
            job_bonus = 7
        elif 50 <= age <= 85 and job_changes <= 4:
            job_bonus = 5
        elif job_changes <= 1:
            job_bonus = 5

        job_penalty = 0
        if job_changes == 2:
            job_penalty = -5
        elif job_changes == 3:
            job_penalty = -10
        elif job_changes >= 5:
            job_penalty = -20

        st_penalty = short_term * 10
        total = age_s + job_bonus + job_penalty - st_penalty + 5

        forced_z = age >= 50  # 年齢50歳以上は他項目に関わらず一律 測定不能(Class-Z)
        if forced_z:
            cn, rc = "測定不能 (Class-Z)", "#ff0000"
        elif total >= 23:
            cn, rc = "優秀 (Class-S)", "#00ff00"
        elif total >= 18:
            cn, rc = "良好 (Class-A)", "#00e5ff"
        elif total >= 13:
            cn, rc = "標準 (Class-B)", "#ffff00"
        elif total >= 8:
            cn, rc = "要努力 (Class-C)", "#ff9900"
        else:
            cn, rc = "測定不能 (Class-Z)", "#ff0000"

        st.markdown(f'<div class="cyber-panel"><h3>判定結果: <span style="color:{rc};">{cn}</span></h3></div>', unsafe_allow_html=True)
        if forced_z:
            st.error("**【エージェント指示】** 優先度：低（年齢50歳以上のため一律 測定不能）")
        elif total >= 15:
            st.success("NICE❕ **【エージェント指示】** 優先度：高")
        elif 7 <= total < 15:
            st.info("safe **【エージェント指示】** 優先度：中")
        else:
            st.error("**【エージェント指示】** 優先度：低")
