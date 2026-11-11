import logging
from typing import Dict, Optional

from sqlalchemy import select

from amocrm import Customer, Lead
from models import AbstractCustomer, AmoCompany, AmoContact, AmoCustomer, AmoLead, db

logger = logging.getLogger(__name__)


def dict_compare_merge(left, right):
    merged = {}
    for item in (left or []) + (right or []):
        entity_id = (item.get('general') or {}).get('id')
        if entity_id is None:
            continue
        previous = merged.get(entity_id, {})
        value = {**previous, **item}
        value['general'] = {**(previous.get('general') or {}), **(item.get('general') or {})}
        merged[entity_id] = value
    return list(merged.values())


def get_customer_json(customer: Customer) -> dict:
    return {
        'general': {
            'id': customer.id,
            'login': customer.get_field('Логин'),
            'billing_type': customer.get_field('Вид оплаты'),
            'is_robot': bool(customer.get_field('Робот')),
            'is_nds': bool(customer.get_field('Оплата с НДС')),
            'system': customer.get_field('Система'),
            'host': customer.get_field('Хост'),
            'name': customer.name,
        },
        'api_value': customer.as_dict(),
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        'general': {'id': lead.id, 'login': lead.get_field('Логин'), 'name': lead.name},
        'api_value': lead.as_dict(),
    }


def _find_by_login(model, login) -> Optional[AbstractCustomer]:
    if not login:
        return None
    ids = db.session.scalars(
        select(model.abstract_customer_id).where(model.login == login).distinct().limit(2)
    ).all()
    if len(ids) > 1:
        logger.warning('Login %r has more than one abstract customer', login)
        return None
    return db.session.get(AbstractCustomer, ids[0]) if ids else None


def get_abccustomer_by_lead_login(login: str) -> Optional[AbstractCustomer]:
    return _find_by_login(AmoLead, login)


def get_abccustomer_by_amocustomer_login(login: str) -> Optional[AbstractCustomer]:
    return _find_by_login(AmoCustomer, login)


def _related(model, aggregate_id, refs):
    for ref in refs or []:
        crm_id = ref.get('id')
        if crm_id is None:
            continue
        row = db.session.get(model, (crm_id, aggregate_id))
        if row is None:
            row = model(crm_id=crm_id, abstract_customer_id=aggregate_id)
            db.session.add(row)
        row.data = {'general': {'id': crm_id, 'name': ref.get('name')}, 'api_value': ref}


def create_or_update_customer(aggregate: Optional[AbstractCustomer], customer: Customer):
    if aggregate is None:
        aggregate = AbstractCustomer()
        db.session.add(aggregate)
        db.session.flush()
    row = db.session.get(AmoCustomer, customer.id)
    if row is None:
        row = AmoCustomer(crm_id=customer.id)
        db.session.add(row)
    row.abstract_customer_id = aggregate.id
    row.data = get_customer_json(customer)
    row.contact_ids = [ref['id'] for ref in customer.contacts or [] if ref.get('id') is not None]
    row.company_ids = [ref['id'] for ref in customer.companies or [] if ref.get('id') is not None]
    _related(AmoContact, aggregate.id, customer.contacts)
    _related(AmoCompany, aggregate.id, customer.companies)
    return aggregate


def create_or_update_lead(aggregate: Optional[AbstractCustomer], lead: Lead,
                          commit: bool = True) -> AbstractCustomer:
    if aggregate is None:
        aggregate = AbstractCustomer()
        db.session.add(aggregate)
        db.session.flush()
    row = db.session.get(AmoLead, lead.id)
    if row is None:
        row = AmoLead(crm_id=lead.id)
        db.session.add(row)
    row.abstract_customer_id = aggregate.id
    row.data = get_lead_json(lead)
    row.contact_ids = [ref['id'] for ref in lead.contacts or [] if ref.get('id') is not None]
    _related(AmoContact, aggregate.id, lead.contacts)
    if commit:
        db.session.commit()
    return aggregate
