from __future__ import unicode_literals

import balanced
from flask import request, redirect, url_for, flash, session
from werkzeug.exceptions import NotFound

from rentmybike import email
from rentmybike.controllers import route, validate, find_form
from rentmybike.db import Session
from rentmybike.forms.purchase import PurchaseForm, GuestPurchaseForm
from rentmybike.models import Listing, User


class RentalManager(object):

    def __init__(self, request):
        super(RentalManager, self).__init__()
        self.request = request

    def rent(self, listing, email_address, card_uri, name=None):
        Session.flush()
        if request.user.is_authenticated:
            user = request.user
        else:
            user = User.create_guest_user(email_address, name)

            # this user has not authenticated with their email/password combo
            # we cannot allow them to charge an existing card if it exists
            if not card_uri:
                raise Exception('Non-authenticated user must supply card data')

        Session.flush()  # ensure there is no db inconsistency
        if not user.account_uri:
            self.create_balanced_account(user, card_uri)
        else:
            if card_uri:
                user.add_card(card_uri)

        Session.flush()  # ensure there is no db inconsistency
        return listing.rent_to(user, card_uri)

    def create_balanced_account(self, user, card_uri):
        # user does not yet have a Balanced account, we need to create
        # this here. this may raise an exception if the data is
        # incorrect or the email address is already associated with an
        # existing account.
        try:
            user.create_balanced_account(card_uri=card_uri)
        except balanced.exc.HTTPError as ex:
            if (ex.status_code == 409 and
                'email_address' in ex.description):
                user.associate_balanced_account()
            else:
                raise ex


@route('/rent', 'rent.index')
def index():
    listings = Listing.query.all()
    return 'rent/index.mako', {
        'listings': listings,
        }


@route('/rent/<listing:listing>', 'rent.show')
def show(listing, purchase_form=None, guest_purchase_form=None, force_form=False):
    is_buyer = False
    account = None

    # if this user is authenticated, then check if they have a Balanced
    # account with the role buyer. if they do then we give them the option
    # to charge this account without entering their card details.
    if (not force_form and
        request.user.is_authenticated and
        request.user.account_uri and
        'buyer' in request.user.balanced_account.roles):
        is_buyer = True
        purchase_form = None
        guest_purchase_form = None
    else:
        purchase_form = purchase_form or PurchaseForm(prefix='purchase',
            obj=request.user)
        guest_purchase_form = guest_purchase_form or GuestPurchaseForm(
            prefix='guest')

    return 'rent/show.mako', {
        'listing': listing,
        'purchase_form': purchase_form,
        'guest_purchase_form': guest_purchase_form,
        'is_buyer': is_buyer,
        'account': account,
        }


@route('/rent/<listing:listing>', 'rent.update', methods=['POST'])
@validate(GuestPurchaseForm, prefix='guest')
@validate(PurchaseForm, prefix='purchase')
def update(listing, **kwargs):
    manager = RentalManager(request)

    forms = kwargs.pop('forms')
    purchase_form = find_form(forms, PurchaseForm)
    guest_purchase_form = find_form(forms, GuestPurchaseForm)
    card_uri = request.form.get('card_uri', None)
    name = None

    if request.user.is_authenticated:
        email_address = request.user.email_address
    else:
        email_address = guest_purchase_form.email_address.data
        name = guest_purchase_form.name.data

    try:
        rental = manager.rent(listing, email_address, card_uri, name)
    except balanced.exc.HTTPError as ex:
        msg = 'Error debiting account, your card has not been charged "{}"'
        flash(msg.format(ex.message), 'error')
        Session.rollback()
    except Exception as ex:
        if ex.message == 'No card on file':
            return show(listing, purchase_form, guest_purchase_form, True)
        raise
    else:
        Session.commit()

        email.send_email(rental.buyer.email_address,
            'Rental Receipt',
            'receipt.mako',
            name=rental.buyer.email_address, listing=listing,
            charge=balanced.Transaction.find(rental.debit_uri)
        )
        session['rental_user_guid'] = rental.buyer.guid
        session['rental_email_address'] = rental.buyer.email_address
        return redirect(url_for('rent.confirmed', listing=listing, rental=rental))
    return show(listing, purchase_form, guest_purchase_form)


@route('/rent/<listing:listing>/confirmed/<rental:rental>', 'rent.confirmed')
def show_confirmed(listing, rental):
    if rental.buyer_guid != session['rental_user_guid']:
        raise NotFound()
    email_address = session['rental_email_address']
    charge = balanced.Transaction.find(rental.debit_uri)
    return 'rent/complete.mako', {
        'listing': listing,
        'rental': rental,
        'charge': charge,
        'email_address': email_address,
        }
