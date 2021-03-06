# -*- coding:utf-8 -*-
import sys
from os.path import getmtime
import logging
import requests
from time import sleep
import datetime
import schedule
import re

from market_maker.market_maker import OrderManager, XBt_to_XBT
from market_maker.settings import settings
from market_maker.utils import log, constants, errors, math
from telegram_msg import tg_send_message, tg_send_important_message

# Used for reloading the bot - saves modified times of key files
import os
watched_files_mtimes = [(f, getmtime(f)) for f in settings.WATCHED_FILES]


#
# Helpers
#
logger = logging.getLogger('root')

class CustomOrderManager(OrderManager):

    def reset(self):
        self.exchange.cancel_all_orders()
        self.sanity_check()
        self.print_status()
        self.position_grade = 0
        self.last_running_qty = 0
        self.cycleclock = 30 // settings.LOOP_INTERVAL
        #仓位等级由0-6级, 按持仓量分级, 每大于order size增加1级, 最高6级
        #持仓方向通过self.running_qty来判断, 大于0为多仓, 小于0为空仓
        schedule.every().day.at("00:00").do(self.write_mybalance) #每天00:00执行一次
        schedule.every(5).seconds.do(self.set_MarkPriceList) #每5秒执行一次
        self.MarkPriceList = []
        marketPrice = self.exchange.get_portfolio()['XBTUSD']['markPrice']
        for x in range(120):
            self.MarkPriceList.append(marketPrice)
        # Create orders and converge.
        with open(r'/root/mybalance.txt', 'r') as f:
            lines = f.readlines()
            m1 = re.match(r'(\d{4}-\d{2}-\d{2})\s(\d{2}\:\d{2}\:\d{2})\s+([0-9\.]+)', lines[-2])
            self.yesterday_balance = float(m1.group(3))
            m2 = re.match(r'(\d{4}-\d{2}-\d{2})\s(\d{2}\:\d{2}\:\d{2})\s+([0-9\.]+)', lines[-3])
            self.before_yesterday_balance = float(m2.group(3))
        self.place_orders()

    def write_mybalance(self):
        now = datetime.datetime.now()
        mybalance = '%.6f' % XBt_to_XBT(self.start_XBt)
        with open(r'/root/mybalance.txt', 'a') as f:
            f.write(now.strftime('%Y-%m-%d %H:%M:%S') + '   ' + mybalance + '\n')
        message = 'BitMEX今日交易统计\n' + \
                '时间：' + now.strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
                '保证金余额：' + mybalance + '\n' + \
                '合约数量：' + str(self.running_qty) + '\n' + \
                '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
                '风险等级：' + str(self.position_grade) + '\n' + \
                '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
                '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice']) + '\n' + \
                '今日盈利：' + '%.6f' % (float(mybalance) - self.yesterday_balance) + '\n' + \
                '作日盈利：' + '%.6f' % (self.yesterday_balance - self.before_yesterday_balance)
        tg_send_important_message(message)
        self.before_yesterday_balance = self.yesterday_balance
        self.yesterday_balance = float(mybalance)

    def set_MarkPriceList(self):
        self.MarkPriceList.pop()
        self.MarkPriceList.insert(0, self.exchange.get_portfolio()['XBTUSD']['markPrice'])

    def get_wave_coefficient(self):
        """求波动系数, 当前市场波动系数, 超过一定值取消挂单"""
        return (max(self.MarkPriceList) - min(self.MarkPriceList))

    def get_position_grade(self):
        """获取仓位等级"""
        
        self.position_grade = abs(self.running_qty) // settings.ORDER_START_SIZE
        if abs(self.running_qty) == settings.ORDER_START_SIZE:
            self.position_grade = 0
        elif self.position_grade > 6:
            self.position_grade = 6
        return self.position_grade

    def get_price_offset2(self, index):
        """根据index依次设置每一个价格，这里为差价依次增大，为上一差价2倍"""
        # Maintain existing spreads for max profit
        if settings.MAINTAIN_SPREADS:
            start_position = self.start_position_buy if index < 0 else self.start_position_sell
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        else:
            # Offset mode: ticker comes from a reference exchange and we define an offset.
            start_position = self.start_position_buy if index < 0 else self.start_position_sell

            # If we're attempting to sell, but our sell price is actually lower than the buy,
            # move over to the sell side.
            if index > 0 and start_position < self.start_position_buy:
                start_position = self.start_position_sell
            # Same for buys.
            if index < 0 and start_position > self.start_position_sell:
                start_position = self.start_position_buy
        if (self.running_qty != 0):
            avgCostPrice = self.exchange.get_position()['avgCostPrice']
            if (avgCostPrice % 1 == 0.5):
                start_position = avgCostPrice
            else:
                start_position = avgCostPrice - 0.25 if index < 0 else avgCostPrice + 0.25
        if index > 0:
            return math.toNearest(start_position + (2 ** index - 1) * settings.OFFSET_SPREAD, self.instrument['tickSize'])
        if index < 0:
            return math.toNearest(start_position - (2 ** abs(index) - 1) * settings.OFFSET_SPREAD, self.instrument['tickSize'])
        if index == 0:
            return math.toNearest(start_position, self.instrument['tickSize'])

    def get_price_offset3(self, index):
        """按仓位等级来设置价格"""
        L = [1, 3, 5, 7, 9, 11, 13, 15, 17]
        if abs(index) > 10:
            logger.error("ORDER_PAIRS cannot over 10")
            self.exit()
        avgCostPrice = self.exchange.get_position()['avgCostPrice']
        if (avgCostPrice % 0.5 == 0):
            start_position = avgCostPrice
        else:
            start_position = avgCostPrice - 0.25 if index < 0 else avgCostPrice + 0.25
        if (index > 0 and start_position < self.start_position_sell):
            start_position = self.start_position_sell
        if (index < 0 and start_position > self.start_position_buy):
            start_position = self.start_position_buy
        if settings.MAINTAIN_SPREADS:
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        print('start_position: %s ' % start_position)
        if index > 0:
            return math.toNearest(start_position + L[index - 1] * 0.5, self.instrument['tickSize'])
        if index < 0:
            return math.toNearest(start_position - L[abs(index) - 1] * 0.5, self.instrument['tickSize'])
        if index == 0:
            return math.toNearest(start_position, self.instrument['tickSize'])

    def place_orders(self):
        """Create order items for use in convergence."""
        buy_orders = []
        sell_orders = []
        order_status = 0
        """order_status参数说明
            0: running_qty为0, 维持原样
            1: self.running_qty > 0, 买卖都变化, 买单按照offset2, 卖单按照offset3
            2: 买单维持不变, 卖单按照offset3
            3: self.running_qty < 0, 买卖都变化, 买单按照offset3, 卖单按照offset2
            4: 卖单维持不变, 买单按照offset3
        """
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        position_grade = self.get_position_grade()
        print ('position_grade: %s ' % position_grade)
        print ('running_qty: %s ' % self.running_qty)
        schedule.run_pending()
        if (abs(self.last_running_qty) > abs(self.running_qty) and self.running_qty > settings.ORDER_START_SIZE):
            if (self.cycleclock == 30 // settings.LOOP_INTERVAL):
                self.send_tg_message()
            self.cycleclock = self.cycleclock - 1
            print('Countdown: %s' % self.cycleclock)
            if (self.cycleclock == 0):
                self.cycleclock = 30 // settings.LOOP_INTERVAL
            else:
                return
        wave_coefficient = self.get_wave_coefficient()
        if(self.running_qty == 0 and wave_coefficient < 8):
            for i in reversed(range(1, settings.ORDER_PAIRS + 1)):
                if not self.long_position_limit_exceeded():
                    buy_orders.append(self.prepare_order(-i, order_status))
                if not self.short_position_limit_exceeded():
                    sell_orders.append(self.prepare_order(i, order_status))
        elif(self.running_qty == 0):
            if (len(self.exchange.get_orders()) != 0):
                self.exchange.cancel_all_orders()
                self.send_tg_message()
            print('wave_coefficient is over 5, Suspend trading!')
            return
        elif(self.running_qty > 0):
            if (self.running_qty == self.last_running_qty):     #持仓不变
                return
            elif (self.running_qty > self.last_running_qty and self.last_running_qty >= 0):     #多仓增加,买单不变,卖单变化offset3
                order_status = 2
                for i in reversed(range(1, (self.running_qty - 1) // settings.ORDER_START_SIZE + 2)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            elif (self.running_qty < self.last_running_qty and self.last_running_qty >= 0):     #多仓减少,卖单不变,买单变化offset2
                order_status = 4
                for i in reversed(range(self.running_qty // settings.ORDER_START_SIZE + 1, settings.ORDER_PAIRS + 1)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
            elif (self.last_running_qty < 0):    #空转多,买卖单都变化,买offset2卖offset3
                order_status = 1
                for i in reversed(range(self.running_qty // settings.ORDER_START_SIZE + 1, settings.ORDER_PAIRS + 1)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
                for i in reversed(range(1, (self.running_qty - 1) // settings.ORDER_START_SIZE + 2)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            else:
                logger.error('running_qty bug. running_qty: %s  last_running_qty: %s' % (self.running_qty, self.last_running_qty))
                self.exit()
        else:
            if (self.running_qty == self.last_running_qty):     #持仓不变
                return
            elif (abs(self.running_qty) > abs(self.last_running_qty) and self.last_running_qty <= 0):     #空仓增加,买单变化offset3,卖单不变
                order_status = 4
                for i in reversed(range(1, (abs(self.running_qty) - 1) // settings.ORDER_START_SIZE + 2)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
            elif (abs(self.running_qty) < abs(self.last_running_qty) and self.last_running_qty <= 0):     #空仓减少,卖单变化offset2,买单不变
                order_status = 2
                for i in reversed(range(abs(self.running_qty) // settings.ORDER_START_SIZE + 1, settings.ORDER_PAIRS + 1)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            elif (self.last_running_qty > 0):    #多转空,买卖单都变化,买offset3卖offset2
                order_status = 3
                for i in reversed(range(1, (abs(self.running_qty) - 1) // settings.ORDER_START_SIZE + 2)):
                    if not self.long_position_limit_exceeded():
                        buy_orders.append(self.prepare_order(-i, order_status))
                for i in reversed(range(abs(self.running_qty) // settings.ORDER_START_SIZE + 1, settings.ORDER_PAIRS + 1)):
                    if not self.short_position_limit_exceeded():
                        sell_orders.append(self.prepare_order(i, order_status))
            else:
                logger.error('running_qty bug. running_qty: %s  last_running_qty: %s' % (self.running_qty, self.last_running_qty))
                self.exit()
        self.last_running_qty = self.running_qty
        print(buy_orders)
        print(sell_orders)
        return self.converge_orders(buy_orders, sell_orders, order_status)

    def prepare_order(self, index, order_status):
        """Create an order object."""

        if settings.RANDOM_ORDER_SIZE is True:
            quantity = random.randint(settings.MIN_ORDER_SIZE, settings.MAX_ORDER_SIZE)
        else:
            quantity = settings.ORDER_START_SIZE + ((abs(index) - 1) * settings.ORDER_STEP_SIZE)
        if(index == 1 or index == -1):
            if ((self.running_qty > 0 and order_status == 4) or (self.running_qty < 0 and order_status == 2)):  #多仓部分减少或空仓部分减少
                quantity = quantity - (abs(self.running_qty) % settings.ORDER_START_SIZE)
            else:
                quantity = quantity + (abs(self.running_qty) % settings.ORDER_START_SIZE)   #仓位化整
        if((order_status == 0) or (order_status == 1 and index < 0) or (order_status == 3 and index > 0) or (order_status == 2 and self.running_qty < 0) or (order_status == 4 and self.running_qty > 0)):
            price = self.get_price_offset2(index)
        elif((order_status == 1 and index > 0) or (order_status == 3 and index < 0) or (order_status == 2 and self.running_qty > 0) or (order_status == 4 and self.running_qty < 0)):
            price = self.get_price_offset3(index)
        else:
            logger.error('Choose offset Error. order_status:%s index:%s self.running_qty:%s' % (order_status, index, self.running_qty))
            self.exit()
        return {'price': price, 'orderQty': quantity, 'side': "Buy" if index < 0 else "Sell"}

    def converge_orders(self, buy_orders, sell_orders, order_status):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            try:
                if (order['side'] == 'Buy' and (order_status == 0 or order_status == 4 or order_status == 3 or order_status == 1)):
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                elif (order['side'] == 'Sell' and (order_status == 0 or order_status == 2 or order_status == 1 or order_status == 3)):
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1
                else:
                    continue

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > settings.RELIST_INTERVAL):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                if ((order_status == 2 and self.running_qty > 0) or (order_status == 1 and self.running_qty > 0) or (order_status == 4 and self.running_qty < 0) or (order_status == 3 and self.running_qty < 0)):
                    to_cancel.append(order)

        if (order_status == 0 or order_status == 4 or order_status == 3 or order_status == 1):
            while buys_matched < len(buy_orders):
                to_create.append(buy_orders[buys_matched])
                buys_matched += 1
        if (order_status == 0 or order_status == 2 or order_status == 1 or order_status == 3):
            while sells_matched < len(sell_orders):
                to_create.append(sell_orders[sells_matched])
                sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
            self.exchange.create_bulk_orders(to_create)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            self.exchange.cancel_bulk_orders(to_cancel)

        if ((len(to_amend) > 0) or (len(to_create) > 0) or (len(to_cancel) > 0)):
            self.send_tg_message()

    def send_tg_message(self):
        now = datetime.datetime.now()
        mybalance = '%.6f' % XBt_to_XBT(self.start_XBt)
        message = 'BitMEX交易状态\n' + \
            '时间：' + now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
            '保证金余额：' + mybalance + '\n' + \
            '合约数量：' + str(self.running_qty) + '\n' + \
            '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
            '风险等级：' + str(self.position_grade) + '\n' + \
            '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
            '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice']) + '\n' + \
            '今日盈利：' + '%.6f' % (float(mybalance) - self.yesterday_balance) + '\n' + \
            '作日盈利：' + '%.6f' % (self.yesterday_balance - self.before_yesterday_balance)
        tg_send_message(message)
        if self.position_grade > 4:
            tg_send_important_message(message)

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        now = datetime.datetime.now()
        message = 'BitMEX交易机器人异常退出\n' + \
            '时间：' + now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
            '合约数量：' + str(self.running_qty) + '\n' + \
            '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
            '风险等级：' + str(self.position_grade) + '\n' + \
            '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
            '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice'])
        tg_send_important_message(message)
        try:
            self.exchange.cancel_all_orders()
            self.exchange.bitmex.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("Unable to cancel orders: %s" % e)

        sys.exit()


def run() -> None:
    order_manager = CustomOrderManager()

    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        order_manager.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()
