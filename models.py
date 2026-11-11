from enum import Enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Computed, ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declared_attr, relationship

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbcCustomerEntities(Enum):
    AmoCustomer = 'amo_customers'
    AmoLead = 'amo_leads'
    AmoCompany = 'amo_companies'
    AmoContact = 'amo_contacts'
    SendMethods = 'send_methods'


class AbstractCustomer(db.Model):
    __tablename__ = 'abstract_customer'
    id = Column(db.BigInteger, primary_key=True)
    send_methods = Column(JSONB)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())
    customers = relationship('AmoCustomer', cascade='all, delete-orphan', passive_deletes=True)
    leads = relationship('AmoLead', cascade='all, delete-orphan', passive_deletes=True)
    contact_entities = relationship('AmoContact', cascade='all, delete-orphan', passive_deletes=True)
    company_entities = relationship('AmoCompany', cascade='all, delete-orphan', passive_deletes=True)

    @property
    def amo_customers(self):
        return [entity.data for entity in self.customers]

    @property
    def amo_leads(self):
        return [entity.data for entity in self.leads]

    @property
    def amo_contacts(self):
        return [entity.data for entity in self.contact_entities]

    @property
    def amo_companies(self):
        return [entity.data for entity in self.company_entities]


class AmoEntityMixin:
    crm_id = Column(db.BigInteger, primary_key=True, autoincrement=False)
    data = Column(JSONB, nullable=False)
    login = Column(db.Text, Computed("data -> 'general' ->> 'login'", persisted=True), index=True)
    name = Column(db.Text, Computed("data -> 'general' ->> 'name'", persisted=True), index=True)
    contact_ids = Column(db.ARRAY(db.BigInteger), nullable=False, server_default='{}')
    company_ids = Column(db.ARRAY(db.BigInteger), nullable=False, server_default='{}')
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())

    @declared_attr
    def abstract_customer_id(cls):
        return Column(db.BigInteger, ForeignKey('abstract_customer.id', ondelete='CASCADE'),
                      nullable=False, index=True)


class AmoCustomer(AmoEntityMixin, db.Model):
    __tablename__ = 'amo_customer'


class AmoLead(AmoEntityMixin, db.Model):
    __tablename__ = 'amo_lead'


class AmoRelatedMixin:
    crm_id = Column(db.BigInteger, primary_key=True, autoincrement=False)
    abstract_customer_id = Column(
        db.BigInteger, ForeignKey('abstract_customer.id', ondelete='CASCADE'),
        primary_key=True,
    )
    data = Column(JSONB, nullable=False)
    name = Column(db.Text, Computed("data -> 'general' ->> 'name'", persisted=True), index=True)


class AmoContact(AmoRelatedMixin, db.Model):
    __tablename__ = 'amo_contact'


class AmoCompany(AmoRelatedMixin, db.Model):
    __tablename__ = 'amo_company'
