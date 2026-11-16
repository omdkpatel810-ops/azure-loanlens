"""The portal's words in English and French. The language comes from ?lang=, then the remembered choice,
then the browser's preference."""

from __future__ import annotations

from fastapi import Request

COOKIE = "loanlens_lang"

TEXT = {
    "en": {
        "title": "LoanLens",
        "tagline": "Upload your loan documents securely. A person reviews every application.",
        "signed_in_as": "Signed in as",
        "sign_out": "Sign out",
        "sign_in": "Sign in",
        "your_applications": "Your applications",
        "none_yet": "You haven't started an application yet.",
        "new_application": "Start an application",
        "loan_type": "Type of loan",
        "mortgage": "Mortgage",
        "car": "Car loan",
        "personal": "Personal loan",
        "amount": "Amount (CAD)",
        "start": "Start",
        "documents": "Documents",
        "no_documents": "No documents yet.",
        "upload": "Upload a document",
        "kind": "What is it?",
        "t4": "T4 slip",
        "pay_stub": "Pay stub",
        "other": "Something else",
        "file": "File (PDF, JPEG or PNG, up to {mb} MB)",
        "send": "Upload",
        "uploaded_ok": "Thanks, we have {name}.",
        "back": "All applications",
        "status": "Status",
        "collecting documents": "Collecting documents",
        "in review": "In review",
        "decided": "Decided",
        "uploaded": "Received",
        "reading": "Being read",
        "read": "Read",
        "needs review": "A person will check it",
        "failed": "We couldn't read it; a person will check it",
        "empty": "That file is empty.",
        "too_big": "That file is over {mb} MB.",
        "wrong_type": "Only PDF, JPEG and PNG files can be uploaded.",
        "locked_pdf": "That PDF is password-protected. Please upload an unlocked copy.",
        "bad_amount": "Enter an amount between $1,000 and $2,000,000.",
        "too_many_applications": "You already have 5 applications open.",
        "too_many_documents": "This application already has 20 documents.",
        "bad_form": "Something went wrong with the form. Please try again.",
        "not_found": "We couldn't find that page.",
        "privacy": "Your documents are stored in Canada, encrypted, and seen only by you and our lending team.",
        "switch": "Français",
        "other_lang": "fr",
    },
    "fr": {
        "title": "LoanLens",
        "tagline": "Téléversez vos documents de prêt en toute sécurité. Une personne examine chaque demande.",
        "signed_in_as": "Connecté en tant que",
        "sign_out": "Se déconnecter",
        "sign_in": "Se connecter",
        "your_applications": "Vos demandes",
        "none_yet": "Vous n’avez pas encore commencé de demande.",
        "new_application": "Commencer une demande",
        "loan_type": "Type de prêt",
        "mortgage": "Prêt hypothécaire",
        "car": "Prêt auto",
        "personal": "Prêt personnel",
        "amount": "Montant (CAD)",
        "start": "Commencer",
        "documents": "Documents",
        "no_documents": "Aucun document pour l’instant.",
        "upload": "Téléverser un document",
        "kind": "De quoi s’agit-il?",
        "t4": "Feuillet T4",
        "pay_stub": "Talon de paie",
        "other": "Autre chose",
        "file": "Fichier (PDF, JPEG ou PNG, {mb} Mo au maximum)",
        "send": "Téléverser",
        "uploaded_ok": "Merci, nous avons reçu {name}.",
        "back": "Toutes les demandes",
        "status": "État",
        "collecting documents": "Documents en cours de réception",
        "in review": "En examen",
        "decided": "Décision rendue",
        "uploaded": "Reçu",
        "reading": "En cours de lecture",
        "read": "Lu",
        "needs review": "Une personne le vérifiera",
        "failed": "Lecture impossible; une personne le vérifiera",
        "empty": "Ce fichier est vide.",
        "too_big": "Ce fichier dépasse {mb} Mo.",
        "wrong_type": "Seuls les fichiers PDF, JPEG et PNG sont acceptés.",
        "locked_pdf": "Ce PDF est protégé par un mot de passe. Veuillez téléverser une copie déverrouillée.",
        "bad_amount": "Entrez un montant entre 1 000 $ et 2 000 000 $.",
        "too_many_applications": "Vous avez déjà 5 demandes ouvertes.",
        "too_many_documents": "Cette demande compte déjà 20 documents.",
        "bad_form": "Un problème est survenu avec le formulaire. Veuillez réessayer.",
        "not_found": "Nous n’avons pas trouvé cette page.",
        "privacy": "Vos documents sont conservés au Canada, chiffrés, et consultés seulement par vous et notre équipe de prêts.",
        "switch": "English",
        "other_lang": "en",
    },
}


def language(request: Request) -> str:
    chosen = request.query_params.get("lang") or request.cookies.get(COOKIE)
    if chosen in TEXT:
        return chosen
    accept = request.headers.get("accept-language", "").lower()
    return "fr" if accept.startswith("fr") else "en"


def money(amount: int, lang: str) -> str:
    if lang == "fr":
        return f"{amount:,}".replace(",", " ") + " $"
    return f"${amount:,}"
