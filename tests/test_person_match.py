"""Tests for scripts/common/person_match.py: resolving "CJ", "pat", "JB" to people in a directory."""

from scripts.common.person_match import initials, match_people, normalize

PEOPLE = [
    {"display_name": "Casey Jordan", "nickname": "Casey Jordan", "uuid": "{u-cj}", "account_id": "acc-cj"},
    {"display_name": "Joaquín Bravo", "nickname": "Joaquin Bravo", "uuid": "{u-jb}", "account_id": "acc-jb"},
    {"display_name": "Pat Duncan", "nickname": "Pat Duncan", "uuid": "{u-pd}", "account_id": "acc-pd"},
    {"display_name": "Pat Holloway", "nickname": "Pat Holloway", "uuid": "{u-ph}", "account_id": "acc-ph"},
    {"display_name": "Jo Burke", "nickname": "jburke", "uuid": "{u-jo}", "account_id": "acc-jo"},
    {"display_name": "Erin Bell (Belle)", "nickname": "Erin Bell", "uuid": "{u-eb}", "account_id": "acc-eb"},
    {"display_name": "Alice Cooper", "nickname": "acooper", "uuid": "{u-ac}", "account_id": "acc-ac"},
]


def names(found):
    return sorted(p["display_name"] for p in found)


class TestNormalize:
    def test_accents_case_and_punctuation(self):
        assert normalize("Joaquín Bravo") == ["joaquin", "bravo"]
        assert normalize("  O'Neil-Smith, J. ") == ["o", "neil", "smith", "j"]

    def test_initials_ignore_parenthetical_nicknames(self):
        assert initials("Casey Jordan") == "cj"
        assert initials("Joaquín Bravo") == "jb"
        assert initials("Erin Bell (Belle)") == "eb"
        assert initials("") == ""


class TestMatchPeople:
    def test_initials(self):
        assert names(match_people("CJ", PEOPLE)) == ["Casey Jordan"]

    def test_initials_can_be_ambiguous_and_all_candidates_are_returned(self):
        assert names(match_people("jb", PEOPLE)) == ["Jo Burke", "Joaquín Bravo"]

    def test_first_name_returns_every_person_with_it(self):
        assert names(match_people("pat", PEOPLE)) == ["Pat Duncan", "Pat Holloway"]

    def test_last_name_and_parenthetical_nickname(self):
        assert names(match_people("jordan", PEOPLE)) == ["Casey Jordan"]
        assert names(match_people("belle", PEOPLE)) == ["Erin Bell (Belle)"]

    def test_exact_display_name_beats_looser_matches(self):
        assert names(match_people("Pat Duncan", PEOPLE)) == ["Pat Duncan"]
        assert names(match_people("pat duncan", PEOPLE)) == ["Pat Duncan"]

    def test_accent_insensitive_exact(self):
        assert names(match_people("joaquin bravo", PEOPLE)) == ["Joaquín Bravo"]
        assert names(match_people("Joaquín Bravo", PEOPLE)) == ["Joaquín Bravo"]

    def test_username_nickname_and_ids(self):
        assert names(match_people("acooper", PEOPLE)) == ["Alice Cooper"]
        assert names(match_people("ACC-AC", PEOPLE)) == ["Alice Cooper"]
        assert names(match_people("{u-pd}", PEOPLE)) == ["Pat Duncan"]

    def test_no_substring_false_positives_and_short_tokens(self):
        assert match_people("ali", PEOPLE) == []  # not a prefix search
        assert match_people("j", PEOPLE) == []  # one letter is not an identity
        assert match_people("", PEOPLE) == []
        assert match_people("nobody", PEOPLE) == []

    def test_too_ambiguous_loose_match_is_not_expanded(self):
        crowd = [{"display_name": f"Sam P{i}", "nickname": f"sp{i}"} for i in range(30)]
        assert match_people("sam", crowd) == []

    def test_people_without_names_are_ignored(self):
        assert match_people("cj", [{}, {"display_name": None}]) == []
