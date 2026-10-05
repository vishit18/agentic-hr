"""Identity resolution rules. Merging two different people does more harm than leaving a duplicate."""
from app.core.models import Candidate, CandidateIdentifier, CandidateVersion
from app.dedup.identity import resolve


def make_candidate(db, name, emails=(), phones=(), employers=(), schools=(), merged_into=None) -> Candidate:
    cand = Candidate(source="cv_folder", merged_into_id=merged_into)
    db.add(cand)
    db.flush()
    data = {
        "name": name,
        "experience": [{"company": c, "title": "x"} for c in employers],
        "education": [{"institution": s} for s in schools],
    }
    ver = CandidateVersion(candidate_id=cand.id, version_no=1, data=data, full_name=name)
    db.add(ver)
    db.flush()
    cand.current_version_id = ver.id
    for e in emails:
        db.add(CandidateIdentifier(candidate_id=cand.id, candidate_version_id=ver.id, kind="email", value=e))
    for p in phones:
        db.add(CandidateIdentifier(candidate_id=cand.id, candidate_version_id=ver.id, kind="phone", value=p))
    db.commit()
    return cand


def incoming(name, emails=(), phones=(), employers=(), schools=()) -> dict:
    return {
        "name": name,
        "emails": list(emails),
        "phones": list(phones),
        "experience": [{"company": c, "title": "x"} for c in employers],
        "education": [{"institution": s} for s in schools],
    }


def test_new_person_when_nothing_matches(db) -> None:
    make_candidate(db, "Alice Example", emails=["alice@x.com"])
    res = resolve(db, incoming("Bob Different", emails=["bob@y.com"]))
    assert res.action == "new"


def test_same_email_auto_links(db) -> None:
    cand = make_candidate(db, "Alice Example", emails=["alice@x.com"])
    res = resolve(db, incoming("Alice E. Example", emails=["alice@x.com", "other@z.com"]))
    assert res.action == "auto_link" and res.candidate_id == cand.id
    assert any("email" in e.lower() for e in res.evidence)


def test_same_phone_auto_links_even_if_name_differs(db) -> None:
    cand = make_candidate(db, "Alice Example", phones=["+97336001234"])
    res = resolve(db, incoming("Alice Smith", phones=["+97336001234"]))
    assert res.action == "auto_link" and res.candidate_id == cand.id


def test_identifiers_matching_two_profiles_is_ambiguous_not_auto_linked(db) -> None:
    a = make_candidate(db, "Alice Example", emails=["alice@x.com"])
    b = make_candidate(db, "A. Example", phones=["+97336001234"])
    res = resolve(db, incoming("Alice Example", emails=["alice@x.com"], phones=["+97336001234"]))
    assert res.action == "ambiguous"
    assert {res.candidate_id, *res.other_candidate_ids} == {a.id, b.id}


def test_merged_away_profiles_are_ignored(db) -> None:
    keep = make_candidate(db, "Alice Example")
    make_candidate(db, "Alice Example", emails=["alice@x.com"], merged_into=keep.id)
    assert resolve(db, incoming("Zed Other", emails=["alice@x.com"])).action == "new"


def test_name_plus_shared_employer_is_possible_match_never_auto(db) -> None:
    cand = make_candidate(db, "Jonathan Doe", emails=["jd@old.com"], employers=["Acme Holdings"])
    res = resolve(db, incoming("Jonathan Doe", emails=["jd@new.com"], employers=["ACME Holdings Ltd"]))
    assert res.action == "possible_match" and res.candidate_id == cand.id
    assert any("employer" in e.lower() for e in res.evidence)
    assert any("name" in e.lower() for e in res.evidence)


def test_similar_name_plus_shared_school_is_possible_match(db) -> None:
    cand = make_candidate(db, "Mohammed Al Khalifa", schools=["University of Bahrain"])
    res = resolve(db, incoming("Mohamed Al-Khalifa", schools=["University of Bahrain"]))
    assert res.action == "possible_match" and res.candidate_id == cand.id


def test_exact_same_name_alone_is_flagged_as_weak_evidence(db) -> None:
    cand = make_candidate(db, "Sara Ahmed", employers=["Globex"])
    res = resolve(db, incoming("Sara Ahmed", employers=["Initech"]))
    assert res.action == "possible_match" and res.candidate_id == cand.id
    assert any("name only" in e.lower() for e in res.evidence)


def test_similar_name_without_corroboration_is_new(db) -> None:
    make_candidate(db, "Sara Ahmed", employers=["Globex"])
    assert resolve(db, incoming("Sarah Ahmad", employers=["Initech"])).action == "new"


def test_single_word_names_never_match_on_name_alone(db) -> None:
    make_candidate(db, "Madonna")
    assert resolve(db, incoming("Madonna")).action == "new"


def test_clearly_different_people_with_a_shared_employer_stay_separate(db) -> None:
    make_candidate(db, "Alice Example", employers=["Acme"])
    assert resolve(db, incoming("Bob Different", employers=["Acme"])).action == "new"


def test_the_candidate_being_updated_is_excluded(db) -> None:
    cand = make_candidate(db, "Sara Ahmed", employers=["Globex"])
    res = resolve(db, incoming("Sara Ahmed", employers=["Globex"]), exclude_candidate_ids={cand.id})
    assert res.action == "new"
