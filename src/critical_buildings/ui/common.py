"""Helpers shared by the UI tabs: the store handle, a refresh counter that resets form widgets, and the save form."""
import streamlit as st

from critical_buildings.control import dumps
from critical_buildings.control.store import ControlError, ControlStore

CATEGORIES_HINT = "e.g. Emergency, Care, Education, Transport, Government Buildings, Miscellaneous"


def store():
    return ControlStore()


def bump():
    st.session_state["v"] = st.session_state.get("v", 0) + 1


def key(name):
    return f"{name}_{st.session_state.get('v', 0)}"


def show_messages(errors, warnings, ok_text="No problems found."):
    if errors:
        st.error("**Fix these before saving:**\n\n" + "\n".join(f"- {e}" for e in errors))
    else:
        st.success(ok_text)
    if warnings:
        with st.expander(f"{len(warnings)} note(s)"):
            for w in warnings:
                st.write("- " + w)


def save_form(label, form_key, can_save, on_save):
    """A reason box + button. on_save(user, reason) does the work; any ControlError is shown, not raised."""
    with st.form(form_key):
        reason = st.text_input("Why are you making this change? (required)", key=key(form_key + "_reason"))
        submitted = st.form_submit_button(label, disabled=not can_save, type="primary")
    if submitted:
        if not reason.strip():
            st.error("Please say why you are making this change.")
            return
        try:
            on_save(st.session_state["user"], reason.strip())
        except ControlError as e:
            st.error(str(e))
            return
        dumps.write_dumps(store())
        bump()
        st.rerun()

